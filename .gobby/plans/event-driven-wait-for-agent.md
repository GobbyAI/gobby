
# Event-Driven wait_for_agent

**Plan ID:** event-driven-wait-for-agent

## Overview
`kind: framing`

Replace `wait_for_agent`'s bounded polling loop with the production
completion-subscription system: register the calling session's lineage for a
durable completion notification, return current run status immediately, and
let `WakeDispatcher` deliver the durable inbox message plus best-effort live
tmux/web/SDK nudge. No timer or cron job. This plan also closes the residual
subscriber leak: `AgentCleanupHandler.post_terminal_cleanup`
(`src/gobby/agents/agent_cleanup.py:148-157`) already removes durable
`completion_subscribers` rows and in-memory registry entries on the
lifecycle-monitor terminal paths, but the caller-by-caller sweep (adversary
rounds 2–5) found the canonical chain bypassed more broadly than the review
doc records. Two bypass classes exist. Producers that notify without
cleanup: callers of `complete_and_notify_agent_run`
(`src/gobby/agents/run_completion.py:16` — the `end_agent_run`
self-termination path and the workflow-enforcement fallback),
`SessionCoordinator.complete_agent_run`
(`src/gobby/hooks/session_coordinator.py:457`, whose
`_notify_agent_completion` at `:741-770` fire-and-forget schedules
`registry.notify` from the hook thread and performs no subscription
cleanup at all), and the no-monitor fallbacks in
`src/gobby/mcp_proxy/tools/agent_cancellation.py` (`:83`, `:123`), which
notify directly with no cleanup. And terminal producers that never notify
at all — the lifecycle monitor's periodic stale-run sweep, the dispatcher
heartbeat's stale-pending reap, kill-side terminalizations reached via
`kill_agent(close_terminal=True)` whose callers issue no follow-up notify,
`unregister_agent`, the HTTP cancel route, the test-mode admin endpoints,
and the spawn/resume failure paths — each of which can strand a registered
`wait_for_agent` waiter until a daemon restart, violating the loss-free
liveness contract
this plan depends on. §1.4 states the terminal-producer contract and
routes every producer through the acknowledged delivery chain (or
documents its code-proven exclusion). This feature multiplies row
creation, so that work lands here rather than as a deferral; the stale
blocker text in `docs/reviews/agents.md:76-80` (which claims no runtime
cleanup exists at all) is corrected in the same change.

Existing infrastructure reused, none of it new: `subscribe_agent_completion`
(`src/gobby/agents/completion_subscribers.py:60`) for lineage expansion +
in-memory registration + durable rows; `CompletionEventRegistry`
(`src/gobby/events/completion_registry.py`) whose `register()` merges on
collision (idempotency); `WakeDispatcher.wake()` (`src/gobby/events/wake.py:111`)
which persists the ISM first and nudges best-effort; restart recovery
(`src/gobby/runner_lifecycle_agents.py`), which reloads durable
subscribers for active runs — §1.3 repositions that reload as an
unconditional first-operation boot step, since today it sits behind
optional services — so a daemon restart mid-wait still wakes the
caller.

Because the caller ends its turn with no timeout fallback, terminal delivery
must be loss-free, and today it is not: `WakeDispatcher.wake` reports ISM
persistence failure only as a return value (`error_code: ism_persist_failed`,
`wake.py:138`), `CompletionEventRegistry.notify` discards that return value
(`completion_registry.py:114-124`), and `post_terminal_cleanup` then
unconditionally sweeps the durable rows — one failed ISM insert strands the
caller permanently. Several terminal payloads (`{"status": "completed"}` at
`agent_cleanup.py:609-611`, `lifecycle_monitor.py:427`) also carry no
`run_id`/`completion_id`, so ISM dedup (`_notification_completion_id`,
`wake.py:586-591`) cannot key them. Terminal producers are also
heterogeneous: the lifecycle-monitor paths await `notify` before cleanup,
but `SessionCoordinator._notify_agent_completion` schedules it
fire-and-forget (`create_task` / `run_coroutine_threadsafe`, never awaited),
so terminal DB state is observable while a notify is still pending, and the
`terminalize_killed_agent_run` error payload
(`agent_cancellation.py:123-127`) carries no `run_id` at all. §1.3 converts
the terminal handoff to an acknowledged one — every terminal payload
carries `run_id`, undelivered subscriber rows are retained instead of
swept, the startup sweep redelivers before removing, and one shared awaited
notify→cleanup helper defines the contract — and §1.4 routes every bypass
producer through that helper.

## Constraints
`kind: framing`

- Pre-0.5.0: no backward compatibility. Callers passing `timeout_seconds` or
  `poll_interval_seconds` fail schema validation and must be updated.
- The tool keeps the name `wait_for_agent`.
- Spawn-time parent auto-subscription (`notify_parent_on_completion`) stays
  enabled and stays best-effort. Only `wait_for_agent`'s subscription path is
  strict, because with no timeout the durable row is the only wake signal that
  survives a daemon restart.
- Live nudges remain best-effort after durable ISM storage. Durable ISM
  storage itself becomes acknowledged (§1.3): a failed persist retains the
  subscriber's durable row for redelivery. ISM dedup (`_notification_exists`)
  is per session+completion and requires the payload to carry
  `completion_id`/`run_id`; §1.3 standardizes that on every terminal payload,
  so repeated `wait_for_agent` calls and sweep redelivery stay idempotent at
  the delivery layer.
- Only `wait_for_agent` converts to event-driven. `wait_for_task`,
  `wait_for_any_task`, and `wait_for_all_tasks` have no implementation — they
  are dead names in `WAIT_TOOL_NAMES`, removed by task #18516.
  `wait_for_summary` intentionally stays poll-based: it is a short
  synchronous content dependency in the compact-continuation flow (the caller
  needs `summary_markdown` inline to proceed in the same turn), and there is
  no notify producer for summary writes — building one would be unjustified
  mechanism. Its cap drift is also covered by #18516 under epic #18497.
- Every task that edits `src/gobby/install/shared/` regenerates
  `src/gobby/install/bundled_content_manifest.json` in the same commit via
  `write_bundled_content_manifest(Path("src/gobby/install"))`
  (`src/gobby/install/manifest.py:85`); parity is enforced by
  `tests/test_build_backend.py::test_committed_bundled_content_manifest_matches_shared_tree`.

## P1: Event-driven core
`kind: framing`

**Goal**: `wait_for_agent(run_id)` subscribes durably and returns immediately;
normal terminal completion cleans up subscriptions.

### 1.1 Add strict persistence, registration outcome, and scoped removal to the subscription helpers [category: code]
`kind: deliverable`

Targets: `src/gobby/agents/completion_subscribers.py`,
`src/gobby/events/completion_registry.py`,
`src/gobby/storage/pipeline_subscribers.py`,
`src/gobby/mcp_proxy/tools/spawn_agent/_factory.py`,
`src/gobby/dispatch/spawn_completion.py`

(The two spawn-path files are consumer-sweep entries only: they are the
direct callers of `subscribe_agent_completion`, verified against the new
signature, and require no edit — the added parameter is keyword-only with a
default and the new return value is ignorable. `dispatch/spawn.py` merely
re-exports the symbol and is intentionally excluded. The only other caller
of `CompletionSubscriberManager.remove_completion_subscribers` is the
pipeline path in `runner_lifecycle_subsystems.py:702`, unchanged by the
optional-parameter addition.)

`subscribe_agent_completion` currently swallows durable-persist errors by
design (spawn must not fail on a subscriber-persist hiccup). `wait_for_agent`
needs persistence to be load-bearing, its completion-race handling (1.2)
needs to know whether registration created the registry entry or merged into
an existing one, and both 1.2 and 1.3 need to remove durable rows for a
*subset* of a run's subscriber sessions. Three changes:

- Add a keyword-only `strict: bool = False` parameter:
  - `strict=False` (default): current behavior, best-effort durable insert,
    errors logged and swallowed. All existing callers
    (`spawn_agent/_factory.py:504`, `dispatch/spawn_completion.py:60`, plus
    the `dispatch/spawn.py` re-export) are behaviorally unchanged and are
    deliberately **not** targets of this deliverable.
  - `strict=True`: durable rows are persisted via
    `CompletionSubscriberManager.add_completion_subscribers` **before**
    in-memory registration, and a persist failure raises a new
    `SubscriptionPersistenceError(RuntimeError)` defined in the same module
    without registering anything in the registry. Persist-first ordering keeps
    the strict error response and the registry state consistent: a failed call
    leaves no live-only subscriber that could still receive a wake. The
    `strict=False` order (in-memory first, best-effort durable) is unchanged.
- `CompletionEventRegistry.register` (`completion_registry.py:47-77`) returns
  `True` when it created a fresh entry and `False` when it merged into an
  existing one. `CompletionSubscriberManager.add_completion_subscribers`
  (`pipeline_subscribers.py:30-43`) switches from fire-and-forget
  `executemany` to a single `INSERT ... ON CONFLICT (completion_id,
  session_id) DO NOTHING RETURNING session_id` statement and returns the
  session ids whose rows it actually created — `ON CONFLICT DO NOTHING`
  otherwise makes pre-existing rows (e.g. retry rows retained after a
  failed delivery, §1.3) indistinguishable from fresh ones, and
  durable-row *ownership* is what 1.2's fresh-branch cleanup keys on.
  `subscribe_agent_completion`'s return value becomes a small result object
  carrying `subscribers` (the expanded lineage, today's list return),
  `created_fresh_entry`, and `inserted_session_ids` (the rows this call
  created; empty when the best-effort `strict=False` insert failed).
  Existing callers ignore the return value and are unchanged.
- `remove_agent_completion_subscribers` (`completion_subscribers.py:92-108`)
  and `CompletionSubscriberManager.remove_completion_subscribers`
  (`pipeline_subscribers.py:60`) gain an optional keyword-only
  `session_ids: list[str] | None = None` filter: `None` keeps today's
  remove-all-rows-for-the-run behavior; a list removes only those sessions'
  rows. 1.2 uses it to discard exactly the caller's own lineage rows in the
  completion race, and 1.3 uses it for delivered-only cleanup.

**Acceptance:**

- 1.1.1 - `subscribe_agent_completion` accepts keyword-only
  `strict: bool = False`; `strict=True` raises `SubscriptionPersistenceError`
  on durable-persist failure; default path is byte-for-byte best-effort as
  today. symbol: `subscribe_agent_completion`. file:
  `src/gobby/agents/completion_subscribers.py`.
- 1.1.2 - Strict-mode raise and default swallow behavior are both covered,
  including: a strict insert failure raises `SubscriptionPersistenceError`
  and leaves no new registry subscriber for the run. test:
  `tests/agents/test_completion_subscribers.py`.
- 1.1.3 - `register()` reports fresh-create vs merge;
  `subscribe_agent_completion` surfaces `created_fresh_entry`. symbol:
  `CompletionEventRegistry.register`. file:
  `src/gobby/events/completion_registry.py`. test:
  `tests/events/test_completion_registry.py`.
- 1.1.4 - The `session_ids` filter removes only the named sessions' rows and
  leaves the rest; omitting it removes all rows for the run as today. symbol:
  `remove_agent_completion_subscribers`. file:
  `src/gobby/agents/completion_subscribers.py`. test:
  `tests/agents/test_completion_subscribers.py`.
- 1.1.5 - `add_completion_subscribers` returns exactly the session ids whose
  rows it created: a pre-existing `(completion_id, session_id)` row is not
  reported, and `subscribe_agent_completion` surfaces the created set as
  `inserted_session_ids`. symbol:
  `PipelineCompletionSubscriberMixin.add_completion_subscribers`. file:
  `src/gobby/storage/pipeline_subscribers.py`. test:
  `tests/events/test_subscriber_storage.py`.

### 1.2 Rewrite wait_for_agent as subscribe-and-return [category: code] (depends: 1.1)
`kind: deliverable`

Target: `src/gobby/mcp_proxy/tools/agents_query_tools.py`,
`src/gobby/mcp_proxy/tools/agents.py`

Replace the poll loop (`agents_query_tools.py:84-125`) with:

```python
@registry.tool(name="wait_for_agent", description=...)
async def wait_for_agent(run_id: str) -> dict[str, Any]:
    run = ctx.runner.get_run(run_id)
    if not run:
        return {"success": False, "error": f"Agent run {run_id} not found"}
    if run.status in agents._TERMINAL_AGENT_STATUSES:
        payload = _agent_result_payload(
            await overlay_live_activity(run, ctx.transcript_reader),
            include_prompt=False)
        return {"success": True, "completed": True,
                "notification_registered": False, **payload}
    # Active run: require session context + completion infrastructure.
    session_id = ctx.get_current_session_id()
    if session_id is None:
        return {"success": False, "error": ...,
                "error_code": "missing_session_context"}
    if ctx.completion_registry is None or ctx.session_manager is None or ctx.db is None:
        return {"success": False, "error": ...,
                "error_code": "completion_services_unavailable"}
    # The active path's ONLY await runs BEFORE the critical region:
    payload = _agent_result_payload(
        await overlay_live_activity(run, ctx.transcript_reader),
        include_prompt=False)

    # INVARIANT (do not break): this handler runs on the registry's owning
    # event loop. The region from the re-read below through the
    # conditional cleanup contains NO await — every call in it (get_run,
    # subscribe_agent_completion, remove_agent_completion_subscribers,
    # cleanup) is synchronous — so no notify coroutine (they all run on
    # this loop) can start, resume, or snapshot subscribers inside it. A
    # notify that already took its snapshot must have started before the
    # region; its DB transition precedes it, so the re-read below observes
    # terminal and returns WITHOUT registering. Writers that CAN
    # interleave with the region are the off-owner-loop DB transition
    # writers (the hook thread, and to_thread/_run_db worker threads —
    # the exhaustive terminal-producer inventory lives in the §1.4
    # terminal-producer contract, deliberately NOT enumerated here).
    # Safety rests on one ordering property, not on any enumeration:
    # every terminal producer commits its DB transition BEFORE its
    # notify/delivery work is scheduled or resumes on this loop — the
    # §1.4 contract makes that hold for every producer, including the
    # previously notify-less sweeps. So the post-registration re-read
    # catches any transition that interleaved with the region, and the
    # corresponding notify cannot snapshot until this region ends — a
    # merged registration is therefore always in that notify's snapshot,
    # and a fresh entry cleaned up here makes that notify a no-op on an
    # unregistered ID. Redundant deliveries are absorbed by ISM dedup
    # (§1.3).
    run = ctx.runner.get_run(run_id)
    if run is None or run.status in agents._TERMINAL_AGENT_STATUSES:
        # Went terminal during the overlay await. Nothing was registered
        # here, so no cleanup decision exists — the canonical
        # notify→cleanup chain (or the sweep) owns every durable row.
        terminal = run
    else:
        try:
            subscription = subscribe_agent_completion(
                completion_registry=ctx.completion_registry, run_id=run_id,
                subscriber_session_id=session_id,
                session_manager=ctx.session_manager, db=ctx.db, strict=True)
        except SubscriptionPersistenceError:
            return {"success": False, "error": ...,
                    "error_code": "subscription_persistence_failed"}
        run = ctx.runner.get_run(run_id)   # off-loop transition race
        terminal = run if (
            run and run.status in agents._TERMINAL_AGENT_STATUSES) else None
        if terminal is not None and subscription.created_fresh_entry:
            # Fresh entry: discard only the durable rows THIS call
            # created (inserted_session_ids, 1.1). Pre-existing rows —
            # e.g. retry rows retained after a failed delivery — are
            # never deleted here; they belong to the sweep (§1.3).
            remove_agent_completion_subscribers(
                db=ctx.db, run_id=run_id,
                session_ids=subscription.inserted_session_ids)
            ctx.completion_registry.cleanup(run_id)    # in-memory entry
        # Merged entry: the pending notify has not run yet (see
        # INVARIANT), so this lineage is in its snapshot — leave the
        # registration for the canonical notify→cleanup cycle.
    # ---- end of no-await critical region ----
    if terminal is not None:
        payload = _agent_result_payload(
            await overlay_live_activity(terminal, ctx.transcript_reader),
            include_prompt=False)
        return {"success": True, "completed": True,
                "notification_registered": False, **payload}
    return {"success": True, "completed": False,
            "notification_registered": True,
            "notification_session_id": session_id, **payload}
```

The owning-loop/no-await invariant above is the load-bearing safety
argument, and its region necessarily starts at the **last status read that
can observe an active run** — not at registration. Four earlier rationales
were each false: "fresh entry ⇒ no pending notify" fails because
`SessionCoordinator` schedules `notify` fire-and-forget after the DB
transition, so terminal status and a pending notify coexist; "no await
between registration and cleanup suffices" fails because the awaited
`overlay_live_activity` previously sat between the first status read and
registration — a notify could start during that await, snapshot
subscribers, and suspend inside the wake dispatch, after which the
resumed call's registration merged *after* the snapshot and the canonical
delivery skipped it; "the hook thread is the only writer that can
interleave with the region" fails because every terminal DB transition
commits off the owning loop, not just the coordinator's — the
`AgentCleanupHandler` terminalizers run complete/cancel/fail/timeout
through `_run_db` on worker threads (`agent_cleanup.py:365-442`,
`444-522`, `541-632`; `_run_db` defaults to `asyncio.to_thread`), and
`complete_and_notify_agent_run` runs `runner.complete_run` via
`asyncio.to_thread` (`run_completion.py:16-46`); and the round-4 repair
of that claim — "the off-loop writers are exactly the coordinator, the
cleanup-handler terminalizers, and `complete_and_notify_agent_run`" —
was itself still false, because the capture-policy default terminalizer
also commits off-loop: `_default_terminalize` runs through
`_async_storage_call` = `asyncio.to_thread` (`capture.py:339-344`,
invoked at `capture.py:452-472`) whenever the capture policy
(`terminate_managed_tmux_async` or a direct `capture_then_kill_async`
call) runs without a `terminalize` callback, and two runtime call sites
omit it — `_close_tmux_session` (`kill.py:296-338`), reached by
`kill_agent(close_terminal=True)` (`kill.py:341-585`) from the MCP kill
tool, the websocket observe-continue handler, dispatch spawn cleanup,
build stop, and daemon shutdown, and resume's
`_kill_spawned_tmux_session` (`resume_executor.py:394-432`), which
calls `capture_then_kill_async(action="fail")` directly from both
`resume_agent_run` failure branches (round 7 staled the round-5
"exactly one omitting call site" form of this very sentence). Two
consecutive rounds proved that any
writer enumeration in a shipped comment goes stale; the safety argument
never needed one. It rests on one ordering property: **the DB status
transition commits before that producer's notify/delivery work is
scheduled or resumes on the registry-owning loop** — the hook thread
schedules its notify task only after the transition returns, and every
async producer awaits its off-thread transition before its notify step
runs; §1.4's terminal-producer contract makes the property contractual
for every producer, including the previously notify-less sweeps it
converts. Any transition that interleaves with the region is therefore
caught by the post-registration re-read, and its notify cannot snapshot
until the region exits. The shipped code must therefore (a) keep every
`await` — both overlay calls included — outside the region, (b) re-read
status as the region's first statement, and (c) state the invariant in
the code comment as the ordering property plus a pointer to the §1.4
terminal-producer contract — never as a writer enumeration, which is
precisely the "exactly" claim that went stale in two consecutive review
rounds; the exhaustive cited inventory lives in this plan and in §1.4.
Fresh-branch cleanup keys on
durable-row *ownership* (`inserted_session_ids` from 1.1), not the full
lineage: `add_completion_subscribers` is `ON CONFLICT DO NOTHING`, so the
`subscribers` list can include sessions whose rows predate this call as
retained retry state (§1.3), and deleting those would erase another
delivery's only redelivery signal.

Supporting changes:

- No context changes: `AgentsRegistryContext`
  (`agents_context.py:30-50`) already exposes `get_current_session_id`,
  `session_manager`, `db`, and `completion_registry`, all wired at the
  existing registry construction site in `agents_registry.py`. The handler
  reuses them directly.
- Delete `_WAIT_FOR_AGENT_MAX_TIMEOUT_SECONDS` from
  `src/gobby/mcp_proxy/tools/agents.py` (line 39) and all timeout/poll/sleep
  machinery from the handler. `_TERMINAL_AGENT_STATUSES` stays.
- The transcript activity overlay (`overlay_live_activity`) is applied to
  every returned payload, including the post-race re-read.
- Tool description rewritten: subscribe once, end the turn, the daemon wakes
  the session with the result; no polling, no timeout.

**Acceptance:**

- 1.2.1 - Public signature is `wait_for_agent(run_id: str)`; timeout and poll
  parameters, the poll loop, and the 1800s constant are gone. symbol:
  `wait_for_agent`. file: `src/gobby/mcp_proxy/tools/agents_query_tools.py`.
- 1.2.2 - Terminal runs return immediately with `completed: true`,
  `notification_registered: false`, no subscription created. test:
  `tests/mcp_proxy/tools/test_agents.py`.
- 1.2.3 - Active runs register in-memory + durable lineage subscribers via
  `subscribe_agent_completion(strict=True)`, re-read status, and return
  `completed: false`, `notification_registered: true`,
  `notification_session_id`; the ambient MCP session UUID from
  `ctx.get_current_session_id()` is the value returned as
  `notification_session_id`. test: `tests/mcp_proxy/tools/test_agents.py`.
- 1.2.4 - Completion-during-registration race returns the terminal result;
  a fresh-created registry entry is removed along with only the durable
  rows this call created (`inserted_session_ids` from 1.1) — never
  pre-existing lineage rows — while a merged entry is left intact for the
  canonical notify → cleanup cycle. test:
  `tests/mcp_proxy/tools/test_agents.py`.
- 1.2.9 - Deterministic late-notify ordering test: the initial status read
  observes an active run; the overlay await is paused; the run transitions
  terminal and its `notify` starts and pauses after taking its subscriber
  snapshot (one pre-existing spawn-time subscriber); the overlay resumes;
  the in-region re-read observes terminal and the call returns the
  terminal result inline WITHOUT registering; `notify` then resumes,
  delivers to its snapshot, and canonical cleanup removes the delivered
  rows — the wait call neither creates nor deletes any durable row. test:
  `tests/mcp_proxy/tools/test_agents.py`.
- 1.2.10 - Deterministic completed-notify ordering test: the initial
  status read observes an active run; the overlay await is paused;
  terminalization runs to completion (DB transition, `notify`, canonical
  cleanup), with one same-lineage durable row left retained by a failed
  delivery; the overlay resumes; the in-region re-read observes terminal
  and the call returns inline without registering; the retained retry row
  is untouched. test: `tests/mcp_proxy/tools/test_agents.py`.
- 1.2.11 - Deterministic in-region transition test (stubbed `get_run`
  returns active at the region's first re-read and terminal at the
  post-registration re-read, standing in for ANY off-owner-loop DB
  transition writer — the hook thread, an `AgentCleanupHandler`
  `_run_db` worker-thread terminalizer,
  `complete_and_notify_agent_run`'s `asyncio.to_thread` transition, or
  the capture-policy default terminalizer (`_default_terminalize` via
  `asyncio.to_thread`, reached by `kill_agent(close_terminal=True)` or
  resume's `_kill_spawned_tmux_session`) —
  all of which commit before their notify/delivery work is scheduled or
  resumes on the owning loop, per the §1.4 terminal-producer contract):
  on the fresh branch, exactly the rows reported in
  `inserted_session_ids` are removed and the registry entry cleaned
  while a planted pre-existing same-lineage retry row survives, and the
  subsequently-run `notify` no-ops on the unregistered ID; on the merged
  branch (pre-existing spawn-time entry), the registration is left
  intact and the subsequently-run `notify` delivers to the merged
  lineage including the waiter. test:
  `tests/mcp_proxy/tools/test_agents.py`.
- 1.2.5 - Missing session context, unavailable completion services, persistence
  failure, and unknown run each return the structured error shapes above.
  test: `tests/mcp_proxy/tools/test_agents.py`.
- 1.2.6 - Registered wake fires on completion: notify on the registry drives
  the wake callback for the subscriber registered by `wait_for_agent`
  (existing `WakeDispatcher` path). test: `tests/events/test_wake_wiring.py`.
- 1.2.7 - Transcript activity overlay assertions preserved for active and
  terminal payloads. test:
  `tests/mcp_proxy/tools/test_agent_live_stats.py`.
- 1.2.8 - HTTP route tests for
  `/api/mcp/gobby-agents/tools/wait_for_agent` updated to the new
  immediate-return contract. test: `tests/servers/test_mcp_routes.py`.

### 1.3 Acknowledged terminal delivery and sweep redelivery [category: code] (depends: 1.1, 1.2)
`kind: deliverable`

Targets: `src/gobby/agents/agent_cleanup.py`, `src/gobby/events/wake.py`,
`src/gobby/events/completion_registry.py`,
`src/gobby/runner_lifecycle_agents.py`,
`src/gobby/agents/run_completion.py`,
`src/gobby/hooks/session_coordinator.py`,
`src/gobby/mcp_proxy/tools/agent_cancellation.py`,
`src/gobby/mcp_proxy/tools/tasks/_expansion_registry.py`,
`src/gobby/runner_lifecycle_subsystems.py`,
`src/gobby/workflows/pipeline_executor_events.py`,
`src/gobby/runner_init/orchestration.py`,
`src/gobby/runner_lifecycle.py`

(The 1.2 dependency is serialization only — 1.2 and 1.3 both extend
`tests/events/test_wake_wiring.py`, so they must not run in parallel.
`runner_lifecycle_subsystems.py` is edited here: `init_subsystems` gains
the first-operation startup-recovery step described below — active-run
subscriber rehydration, then the acknowledged sweep. The other six
trailing target files are consumer-sweep entries only: they are the
direct callers of `CompletionEventRegistry.notify` — whose new return value
is ignorable — and, for `runner_init/orchestration.py:147`, the wiring site
that passes `WakeDispatcher.wake` as the registry callback. None of those
six is edited here; `run_completion.py`, `session_coordinator.py`, and
`agent_cancellation.py` are edited by §1.4, which depends on 1.3, so the
shared ownership is serialized. `runner_lifecycle.py` is likewise a
consumer-sweep entry only — the `init_subsystems` launch site
(`runner_lifecycle.py:226-232`); the function's signature and launch are
untouched, so it is not edited here.)

Makes the terminal handoff loss-free (the Overview's stranding scenario):

- **Every terminal payload carries `run_id`.**
  `AgentCleanupHandler.notify_terminal_completion`
  (`agent_cleanup.py:102-115`) injects `run_id` into the result payload when
  absent — one central fix that covers the bare `{"status": "completed"}` /
  `{"status": "error", ...}` payloads (`agent_cleanup.py:609-611`,
  `lifecycle_monitor.py:427`) and every future caller, making
  `_notification_completion_id` resolve and ISM dedup effective for all
  terminal notifications.
- **Delivery status propagates, and the classifier is total and
  conservative.** `WakeDispatcher.wake` result gains a first-class
  `ism_persisted: bool` (today derivable only from
  `error_code == "ism_persist_failed"`); `CompletionEventRegistry.notify`
  collects per-subscriber wake results and returns a
  `{session_id: delivered}` map instead of discarding them. `delivered`
  is `True` **only** when the wake callback returned a mapping with
  `ism_persisted` true (fresh insert or dedup-suppressed duplicate) or
  with `error_code == "session_not_found"` (terminal — nothing will ever
  consume a deleted session's row). **Every other outcome maps to
  `False`**: the registry was constructed with `wake_callback=None`
  (`completion_registry.py:39` explicitly allows it), the callback raised,
  returned `None` or a non-mapping, or returned a mapping lacking
  `ism_persisted` (`WakeCallback`'s result type is an unconstrained
  `object`, `completion_registry.py:18`). Undelivered means the durable
  row is retained: an unknown outcome must never be classified as
  delivered, because deleting on it could erase a subscriber's only
  redelivery state.
- **One awaited notify→cleanup contract, shared by every producer.** A new
  module-level coroutine `deliver_and_cleanup_terminal_run(...)` in
  `agent_cleanup.py` becomes the single terminal-delivery entry point:
  inject `run_id` into the payload when absent, `await notify(...)` for the
  delivery map, remove durable rows only for delivered sessions
  (`session_ids` filter from 1.1), then `completion_registry.cleanup`.
  `AgentCleanupHandler.notify_terminal_completion` /
  `post_terminal_cleanup` are reimplemented on top of it for the
  lifecycle-monitor paths, and §1.4 routes every bypass producer through
  it. Notify and cleanup are ordered inside one awaited chain, so no
  producer can clean before delivery regardless of how it schedules the
  chain.
- **Defined behavior with no delivery map.** `notify` returns `None` when
  it no-ops (duplicate notification or unregistered ID). In that case the
  helper removes **no** durable rows — the canonical chain that did deliver
  (or the startup sweep) owns them — and still calls
  `completion_registry.cleanup`, which is an idempotent pop. `post_terminal_
  cleanup` therefore never guesses: rows are removed only against an actual
  delivery map. Undelivered sessions' rows are retained as retriable
  durable state. `wait()` eviction semantics
  (`CompletionResultEvictedError`) are unchanged, and redelivery does not
  need the registry.
- **The startup sweep redelivers, then removes only what it delivered.**
  `_cleanup_terminal_agent_completion_subscribers`
  (`runner_lifecycle_agents.py:36-51`) becomes acknowledged
  deliver-then-remove: for each terminal run with remaining durable rows,
  synthesize the terminal payload from the run record (`status`, `run_id`),
  `wake()` each subscriber session (per-completion ISM dedup suppresses
  duplicates for sessions that were already delivered), and remove only the
  rows whose redelivery durably persisted (`ism_persisted`) or whose
  session no longer exists (`session_not_found` is terminal — nothing will
  ever consume that row). A row whose redelivery fails again is retained
  and retries on the next startup sweep. Runs that went terminal while the
  daemon was down and runs stranded by an ISM-persist failure both reach
  the subscriber eventually. **The sweep is an unconditional startup
  step.** Today the sweep call sits inside
  `_recover_agent_runs_after_restart` (`runner_lifecycle_agents.py:59`),
  whose only production caller runs behind
  `_start_agent_lifecycle_monitor`'s monitor-is-None early return
  (`runner_lifecycle_subsystems.py:388-390`; monitor construction
  degrades to None on failure, `runner_init/orchestration.py:181-200`),
  and the sweep itself no-ops when `pipeline_execution_manager` is None
  (`runner_lifecycle_agents.py:38-40`; pipeline initialization is
  fail-open, `runner_init/orchestration.py:150-165`) — while strict
  registrations write durable rows straight through
  `CompletionSubscriberManager(db)` (`completion_subscribers.py:75-82`),
  independent of both, so a committed terminal row with retained rows
  could otherwise sit unswept on a degraded boot. Position matters as
  much as unconditionality: `init_subsystems` runs as a background task
  (`runner_lifecycle.py:226-232`) whose done callback only logs an
  uncaught failure (`_log_subsystem_init_result`,
  `runner_lifecycle_startup.py:48-66`), and the operations it awaits
  ahead of the monitor start are optional and fallible —
  `_check_external_services`, for one, awaits its Qdrant and FalkorDB
  probes and reconfigures graph clients with no outer exception
  boundary (`runner_lifecycle_subsystems.py:116-149`) — so a sweep
  sequenced anywhere after them dies with the first uncaught startup
  exception while the daemon keeps serving HTTP. Recovery therefore
  becomes the **first executable operation** in `init_subsystems`
  (`runner_lifecycle_subsystems.py:789-854`), and it is a two-step
  boot recovery, not the sweep alone. Durable rows are not only
  terminal-run state: a strict `wait_for_agent` row whose run is
  still active at boot must reach the in-memory registry, or a
  terminal transition on the serving daemon notifies an empty
  subscriber list, the already-finished terminal sweep never
  re-examines the run, and delivery strands until yet another
  restart. Today that active-run reload sits behind the same
  optional gates as the old sweep: `_start_agent_lifecycle_monitor`
  returns before reconciliation when the monitor is None
  (`runner_lifecycle_subsystems.py:388-390`),
  `_recover_agent_runs_after_restart` returns unless `agent_runner`
  and `completion_registry` exist, and it reads durable rows only
  through `pipeline_execution_manager`
  (`runner_lifecycle_agents.py:60-61,79-81`) — fail-open at init
  (`runner_init/orchestration.py:150-165`). Step one therefore
  rehydrates active runs: for every active run (`list_active` spans
  `running` and `pending`, `storage/agents/_queries.py:197-206`),
  the run's durable `completion_subscribers` rows are read through
  the same directly-constructed `CompletionSubscriberManager` and
  land — with the run's `continuation_prompt`, via merge-semantics
  registration — in `runner.completion_registry`, which is
  constructed unconditionally beside the `WakeDispatcher` ahead of
  every fail-open init block (`runner_init/orchestration.py:139-148`).
  Step two is the acknowledged sweep exactly as above, through the
  same manager plus a `LocalAgentRunManager` for the terminal-run
  records, with the old call site inside
  `_recover_agent_runs_after_restart` dropped. Rehydration precedes
  the sweep so a run terminalizing concurrently during boot is
  covered from both sides: a transition after registration reaches
  its waiter through the live notify, and a transition before or
  during registration (whose notify no-ops as unregistered, rows
  retained) is terminal by the time the sweep queries, so the sweep
  delivers it. The pipeline-gated subscriber loading inside
  `_recover_agent_runs_after_restart` goes away with the old sweep
  call site; the monitor-gated reconciliation keeps its remaining
  duties, and its `is_registered` skip
  (`runner_lifecycle_agents.py:77-78`) plus the registry's merge
  semantics keep its residual registration idempotent against step
  one for any interleaving. A boot with no lifecycle monitor, a
  boot with no pipeline runtime, and a boot where a later optional
  startup operation raises all rehydrate active-run subscribers and
  replay retained rows before anything can fail. One existing path is a *designed* client of
  this sweep and stays on it (documented, not converted, by §1.4):
  `_cancel_active_agent_runs_for_shutdown`
  (`runner_lifecycle_agents.py:238-273`) — safe by the branch-complete
  argument stated in full in §1.4: the durable rows were persisted earlier
  by strict `wait_for_agent` registration, shutdown removes a row only
  after its live delivery is acknowledged (the helper's delivered-map
  rule), and failed deliveries and the branch that skips the live notify
  both leave their rows for this sweep. Daemon-down staleness needs no
  out-of-process client: `gobby agents cleanup` performs no direct-DB
  terminal writes in any daemon state (§1.4), so runs that go stale while
  the daemon is down stay active until the next boot's stale
  pending/running sweeps terminalize them in-daemon, and those in-daemon
  callers route every transitioned run through acknowledged delivery
  (§1.4's 1.4.9 wiring). **Liveness contract, stated explicitly:**
  after an in-process ISM-persist failure the retry trigger is the next
  daemon restart's sweep — a deliberate operational dependency; this plan
  adds no timers or cron by design (see Overview), and the durable row is
  the retry state that makes the restart-triggered retry correct.

**Acceptance:**

- 1.3.1 - `notify_terminal_completion` injects `run_id` into every terminal
  payload; ISM dedup keys resolve for success, error, and cancel paths.
  symbol: `AgentCleanupHandler.notify_terminal_completion`. file:
  `src/gobby/agents/agent_cleanup.py`. test:
  `tests/agents/test_agent_cleanup.py`.
- 1.3.2 - `wake` exposes `ism_persisted`; `notify` returns the per-session
  delivery map with the total conservative classifier pinned: delivered
  only for `ism_persisted` true or `session_not_found`; undelivered for a
  raising callback, a `None`/non-mapping/`ism_persisted`-less result, and
  for a registry constructed with `wake_callback=None` — each without
  affecting other subscribers. file:
  `src/gobby/events/completion_registry.py`. test:
  `tests/events/test_completion_registry.py`.
- 1.3.3 - ISM insert failure for one subscriber retains that subscriber's
  durable row and removes the delivered subscribers' rows (via the 1.1
  `session_ids` filter); with no delivery map (duplicate notify /
  unregistered ID) no rows are removed and the registry entry is still
  cleaned. symbol: `AgentCleanupHandler.post_terminal_cleanup`. file:
  `src/gobby/agents/agent_cleanup.py`. test:
  `tests/agents/test_agent_cleanup.py`.
- 1.3.4 - The startup sweep delivers the synthesized terminal payload to
  sessions with retained rows, dedups against already-delivered
  notifications, and removes only acknowledged rows: the chain initial ISM
  failure → sweep redelivery fails again (row retained) → later sweep
  succeeds (row delivered and removed) is covered end-to-end, as is
  `session_not_found` row removal. Deterministic next-boot cases pin the
  first-operation invocation: with `agent_lifecycle_monitor` None, with
  `pipeline_execution_manager` None, and with the next optional startup
  operation raising (the background init task fails after the recovery
  step), startup still runs the recovery first and a retained row
  reaches acknowledged delivery before the failure surfaces. test:
  `tests/test_runner_lifecycle.py`.
- 1.3.5 - Wake wiring drives the registered subscriber callback for
  `wait_for_agent` registrations under the new notify return contract, with
  the **real** `WakeDispatcher` results (not callback fakes) flowing into
  `notify`'s delivered map. test: `tests/events/test_wake_wiring.py`.
- 1.3.6 - `WakeDispatcher.wake`'s result contract is covered at its owner:
  `ism_persisted` is true for a successful ISM insert and for a
  dedup-suppressed duplicate, false for an ISM insert failure; the
  `session_not_found` result shape is pinned for the delivered-map policy.
  symbol: `WakeDispatcher.wake`. file: `src/gobby/events/wake.py`. test:
  `tests/events/test_wake.py`.
- 1.3.7 - `deliver_and_cleanup_terminal_run` orders cleanup strictly after
  the awaited `notify` in one chain, injects `run_id`, honors the
  no-delivery-map rule, and retains rows for every subscriber the
  classifier marks undelivered — covered per classifier case: no-callback
  registry, raising callback, non-mapping result, `ism_persisted`-less
  mapping. symbol: `deliver_and_cleanup_terminal_run`. file:
  `src/gobby/agents/agent_cleanup.py`. test:
  `tests/agents/test_agent_cleanup.py`.
- 1.3.8 - Active-run rehydration is unconditional and precedes the
  sweep: with `agent_lifecycle_monitor` None and with
  `pipeline_execution_manager` None, a boot where a durable subscriber
  row belongs to a still-active run rehydrates that subscriber into
  the completion registry as part of the first executable operation,
  and when the run goes terminal later — with no further restart — the
  subscriber reaches acknowledged delivery; a companion case pins the
  boot-concurrent transition (terminal commit landing between the
  rehydration snapshot and registration) reaching its subscriber via
  the subsequent sweep; and a monitor-present boot pins
  reconciliation's residual registration as idempotent against step
  one — no dropped waiter, no duplicate delivery. symbol:
  `init_subsystems`. file: `src/gobby/runner_lifecycle_subsystems.py`.
  test: `tests/test_runner_lifecycle.py`.

### 1.4 Close the completion-subscription leak on bypass terminal paths [category: code] (depends: 1.2, 1.3)
`kind: deliverable`

Targets: `src/gobby/mcp_proxy/tools/agents_termination.py`,
`src/gobby/workflows/engine/enforcement_completion.py`,
`src/gobby/agents/run_completion.py`,
`src/gobby/hooks/session_coordinator.py`,
`src/gobby/mcp_proxy/tools/agent_cancellation.py`,
`src/gobby/mcp_proxy/tools/agents_lifecycle_tools.py`,
`src/gobby/agents/agent_cleanup.py`,
`src/gobby/agents/lifecycle_monitor.py`,
`src/gobby/agents/capture.py`,
`src/gobby/storage/agents/_cleanup.py`,
`src/gobby/storage/agents/_lifecycle.py`,
`src/gobby/storage/executor.py`,
`src/gobby/storage/hub/postgres.py`,
`src/gobby/events/wake.py`,
`src/gobby/runner_init/orchestration.py`,
`src/gobby/dispatch/dispatcher.py`,
`src/gobby/dispatch/spawn_actions.py`,
`src/gobby/dispatch/spawn.py`,
`src/gobby/scheduler/executor.py`,
`src/gobby/build/controls.py`,
`src/gobby/servers/websocket/handlers/session_observe_continue.py`,
`src/gobby/servers/websocket/server.py`,
`src/gobby/servers/websocket/session_control.py`,
`src/gobby/runner_init/servers.py`,
`src/gobby/servers/routes/agents.py`,
`src/gobby/servers/routes/agent_spawn.py`,
`src/gobby/servers/routes/admin/_testing.py`,
`src/gobby/mcp_proxy/tools/agents_query_tools.py`,
`src/gobby/mcp_proxy/tools/spawn_agent/_failure_cleanup.py`,
`src/gobby/mcp_proxy/tools/spawn_agent/_health.py`,
`src/gobby/mcp_proxy/tools/spawn_agent/_factory.py`,
`src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`,
`src/gobby/agents/resume_executor.py`,
`src/gobby/dispatch/daemon_resume.py`,
`src/gobby/cli/agents.py`,
`src/gobby/runner_lifecycle_agents.py`,
`src/gobby/runner_gate.py`,
`src/gobby/runner_lifecycle_shutdown.py`,
`src/gobby/runner_lifecycle.py`,
`src/gobby/runner.py`,
`docs/reviews/agents.md`

(Shared-file serialization: `agent_cleanup.py` is a 1.3 target and
`agents_query_tools.py` is a 1.2 target; 1.4 depends on both, so ownership
is serialized through the existing depends chain.
`runner_lifecycle_agents.py` is likewise a 1.3 target — its startup-sweep
conversion — and is edited here too: the shutdown-exclusion branch pin
(1.4.14) plus the definition of the exclusive-fence acquisition helper
the boot gate awaits (1.4.21); the shared ownership is
serialized through the same depends chain.
`events/wake.py` is likewise a 1.3 target and is edited here too: the
bounded delivery statements and the live-nudge deadline (1.4.19,
1.4.21). `runner_init/orchestration.py` is a 1.3 consumer-sweep entry,
not edited there; here it is edited — the instance-local
dispatcher-executor wiring (1.4.20); the module-seam supply and
admission lifecycle live in `run_daemon`'s post-claim activation
step, not in this init block. Both ownerships are serialized
through the same depends chain.
`runner_lifecycle_shutdown.py` is edited here: the graceful phase gains
producer quiescence, `_run_async_shutdown_cleanup` gains the admission
close plus shielded-delivery drain, and `shutdown_daemon_services`'
finalizer gains the owned, cancellation-armored settlement sequence —
admission close, drain, barrier — all described
below (1.4.18–1.4.21). `runner_lifecycle.py` is edited here too:
`run_daemon` gains the awaited boot-fence gate between the pid claim
and Uvicorn server creation, with its fatal-abort handling (1.4.21),
and the post-claim lifecycle-activation step — module-seam supply,
empty-set assertion, admission reopen (1.4.20) — while its embedded
claim block is deleted: ownership is resolved by callers before
`GobbyRunner` construction and carried as a required, non-default
parameter of `GobbyRunner.run` and `run_daemon` (1.4.20). `runner.py`
is edited here too: `main()`'s expiry-branch exit backstop (1.4.19),
`GobbyRunner.__init__`'s construction rollback ledger and its
last-in-first-out unwind, and
`run_gobby`'s pre-construction pid claim with its idempotent
post-rollback failure-path release (1.4.20).
The `shutdown_daemon_services` call site is untouched.)

Baseline (corrected in enhancement round 1): subscription cleanup on normal
terminal completion already exists.
`AgentCleanupHandler.post_terminal_cleanup` (`agent_cleanup.py:148-157`)
removes the run's durable `completion_subscribers` rows and calls
`completion_registry.cleanup(run.id)`, and every lifecycle-monitor terminal
path — including `terminalize_successful_run` (`agent_cleanup.py:365-442`),
which notifies then cleans — reaches it. That behavior is reused (as
reimplemented over the 1.3 helper).

**The terminal-producer contract.** Every code path that moves an
`agent_runs` row to a terminal status must end with that run entering the
acknowledged delivery chain — `deliver_and_cleanup_terminal_run` (1.3) or
a canonical path reimplemented on it — or carry a code-proven exclusion
whose delivery falls to the next-boot startup sweep (1.3). The helper is
idempotent (ISM dedup plus the no-delivery-map rule), so a producer that
cannot know whether another path already delivered awaits it anyway. The
contract additionally presumes terminalize calls are atomic — a
terminal-transition call that raises has committed nothing (Fix step
zero below); without that, no helper placement after a terminalize
return can be sound. That presumption is additionally a same-thread
guarantee only: every async producer reaches those calls through an
`asyncio.to_thread` bridge — the cleanup handler's and monitor's
`_run_db` defaults (`agent_cleanup.py:97-100`,
`lifecycle_monitor.py:216-219`), the complete-run offload inside
`complete_and_notify_agent_run` (`run_completion.py:16-46`), and
capture's `_async_storage_call` (`capture.py:339-344`) — and once
the worker thread has started, cancelling the awaiting task does not
stop the worker: the await raises `asyncio.CancelledError` (a direct
`BaseException` subclass on Python 3.13, invisible to every
`except Exception` boundary) while the worker runs on and commits
the terminal UPDATE. The cancellation sources are
production-reachable — `AgentLifecycleMonitor.stop` cancels its loop
task (`lifecycle_monitor.py:289-299`) and daemon shutdown cancels
live HTTP/MCP request tasks
(`runner_lifecycle_shutdown.py:211-226`) — so the Fix routes every
async terminal producer through the cancellation-shielded delivery
scope defined below. The round-5 caller-by-caller sweep verified this
inventory in two bypass classes.

Producers that notify without cleanup:

- Callers of `complete_and_notify_agent_run` (`run_completion.py:16`):
  `end_agent_run` self-termination
  (`_complete_self_terminated_run`, `agents_termination.py:142` tmux
  terminalize callback and `:171` non-tmux branch — its
  `_cleanup_terminal_artifacts` at `:62` cleans runtime state and nothing
  subscription-related), and the workflow-enforcement completion fallback
  (`enforcement_completion.py:189-202`), taken only when the runner exposes
  no async `terminalize_successful_run`.
- `SessionCoordinator.complete_agent_run`
  (`session_coordinator.py:457-638`): runs synchronously on the hook
  thread, transitions the run via `_terminate_agent_run`, then
  `_notify_agent_completion` (`:741-770`) schedules `notify`
  fire-and-forget (`create_task` or `run_coroutine_threadsafe`) with no
  cleanup — durable rows leak and cleanup elsewhere can race the pending
  notify.
- The no-monitor fallbacks in `agent_cancellation.py`:
  `terminalize_cancelled_agent_run` (`:83`) notifies with no cleanup, and
  `terminalize_killed_agent_run`'s error branch (`:123`) notifies a payload
  with no `run_id` and no cleanup.

Producers that terminalize and never notify at all — each strands a
registered `wait_for_agent` waiter until a daemon restart:

- **Periodic sweeps.** The lifecycle monitor's `_check_loop` awaits
  `_run_db(cleanup_stale_runs)` every 10th iteration
  (`lifecycle_monitor.py:325-330`; `_run_db` defaults to
  `asyncio.to_thread`, `:216-219`); `cleanup_stale_runs`
  (`storage/agents/_cleanup.py:29-117`) transitions matching `running`
  rows (`pid IS NULL AND tmux_session_name IS NULL`, activity timeout
  exceeded) to `timeout` via `self.timeout` and returns only a count. The
  stale-pending reaps do the same for `pending` rows: the dispatcher
  heartbeat calls `cleanup_stale_pending_runs` on every run
  (`dispatch/dispatcher.py:195-197`), and the cleanup-handler wrapper
  (`agent_cleanup.py:534-538`) is reached from the monitor
  (`lifecycle_monitor.py:692-694`) and at startup after restart
  reconciliation (`runner_lifecycle_subsystems.py:406`, by which point
  the first-operation boot recovery (1.3) has already rehydrated
  active-run registry entries — `list_active` covers `running` and
  `pending`, `storage/agents/_queries.py:197-206`).
- **Kill-path callers with no follow-up notify.**
  `kill_agent(close_terminal=True)` commits the run's transition through
  the capture-policy default terminalizer (§1.2 proof), and three runtime
  callers stop there: the websocket observe-continue handler
  (`servers/websocket/handlers/session_observe_continue.py:41-57`),
  dispatch's `cleanup_unattached_spawned_run`
  (`dispatch/spawn_actions.py:178-220`, which also re-fails directly via
  `run_storage.fail` at `:208` and returns `False` on kill exception or
  kill failure before reaching that fail — `spawn_actions.py:192-205` —
  while its `close_terminal=True` kill may already have capture-committed
  the transition), and build stop's `_cancel_active_agents`
  (`build/controls.py:501-532`) — whose monitor branch silently skips
  notify because `terminalize_cancelled_run` notifies only when its own
  compare-and-set performed the transition (`transitioned_here`,
  `agent_cleanup.py:481-489`) and the preceding kill already had, and
  whose no-monitor fallback is a bare `run_manager.cancel` (`:530`). The
  MCP kill surfaces are in this class too, on the capture-preempted path
  (round 8 falsified the round-5 "NOT in this class" parenthetical):
  `stop_agent_run` (`agent_cancellation.py:154-221`) kills with
  `close_terminal=True` before `terminalize_cancelled_agent_run`, and
  when the capture-policy default terminalizer commits the cancellation
  first, both terminalize branches — the monitor CAS
  (`transitioned_here` false) and the no-monitor `runner.cancel_run` —
  report no transition and skip notify (`agent_cancellation.py:53-92`);
  MCP `kill_agent` (`agents_lifecycle_tools.py:156-286`) kills with
  `close_terminal=not debug`, hits the same preemption before
  `terminalize_killed_agent_run`, and with `stop=False` returns before
  any terminalize step at all — a capture-committed transition with no
  delivery. With `stop=True` (the default) it continues into
  `terminalize_killed_agent_run` (`agent_cancellation.py:95-151`),
  where the same preemption makes both request shapes silent no-ops:
  the cancelled branch's `terminalize_cancelled_agent_run` reports no
  transition, and the error branch's `runner.run_storage.fail` returns
  `None` on the already-terminal row — each only debug-logged, with no
  delivery. And independent of `stop`, both MCP surfaces share a
  kill-failure early return: any `success=False` kill result without
  `KILL_ERROR_NO_TARGET_PID` returns before any terminalize or re-read
  (`agent_cancellation.py:177-188`, `agents_lifecycle_tools.py:249-264`),
  yet `kill_agent` closes the terminal first (`_close_tmux_session`,
  `agents/kill.py:371-381`) and can fail afterward — e.g. "Terminal
  closed but no target PID was found to verify process death"
  (`kill.py:473-487`) — so the capture terminalizer may already have
  committed the transition, stranding a waiter on an ordinary
  failure-result branch.
- **`unregister_agent`** (`mcp_proxy/tools/agents_query_tools.py:296-304`)
  → `runner.cancel_run` (`agents/runner_queries.py:59-81`): storage
  cancel plus session status update, no notify.
- **The HTTP cancel route.** `POST /api/agents/runs/{run_id}/cancel`
  (`create_agents_router.cancel_agent_run`,
  `servers/routes/agents.py:717-744`) kills via
  `kill_agent(run, db, signal_name="TERM")` — default
  `close_terminal=False`, so no capture-path terminalize runs — then
  `_reconcile_cancelled_agent_run` (`:40-55`) commits the transition
  through a bare `LocalAgentRunManager.cancel` (retried once on
  exception). No notify or delivery follows.
- **Test-mode admin endpoints** (`register_testing_routes`):
  `register_test_agent` (`servers/routes/admin/_testing.py:119-189`)
  creates a fresh `agent_runs` row and can synchronously terminalize it
  via direct `arm.complete`/`cancel`/`fail`/`timeout`, and
  `unregister_test_agent` (`:192-235`) directly `fail`s an existing —
  possibly active and subscribed — run. Test-mode-only is not a
  code-proven no-subscriber exclusion, and neither is the fresh-row
  window: `create` and the terminal write commit in separate
  transactions, so a strict `wait_for_agent` registration can land
  between them.
- **Spawn/resume failure paths**: `_fail_run`
  (`mcp_proxy/tools/spawn_agent/_failure_cleanup.py:91-109`),
  `_deferred_tmux_health_check`
  (`mcp_proxy/tools/spawn_agent/_health.py:81-115`), and resume's
  `_fail_run` (`agents/resume_executor.py:462-466`).
- **Resume's capture-policy kill helper.** `_kill_spawned_tmux_session`
  (`agents/resume_executor.py:394-432`) calls
  `capture_then_kill_async(action="fail")` with no `terminalize`
  callback, so the capture-policy default terminalizer
  (`_default_terminalize`, `capture.py:452-472`) commits the error
  transition off-thread with no notify. Both `resume_agent_run` failure
  branches reach it (`resume_executor.py:236-264`): the
  runtime-persist-failure branch calls `_fail_run` only when no tmux
  session was spawned, and the start-skipped branch never calls
  `_fail_run` at all — so a tmux-spawned resume failure terminalizes
  exclusively through this helper. It also swallows every capture-policy
  exception, so a policy failure before its transition can leave the run
  active with no terminal producer at all.
- **Out-of-process CLI**: removed as a producer class by this plan.
  `gobby agents cleanup` (`cli/agents.py:826-862`) today runs
  `cleanup_stale_runs` and `cleanup_stale_pending_runs` directly against
  the DB from a separate process where no in-daemon notify can ever
  follow; the Fix strips those direct-DB terminal writes outright
  (non-dry-run routes through `POST /api/agents/cleanup`, 1.4.11), so
  post-plan the CLI performs no terminal write in any daemon state and
  daemon-down staleness resolves through the two-stage chain: the stale
  run is left untouched, and the next boot's in-daemon sweeps
  terminalize it and enter acknowledged delivery (1.4.9).

Code-proven exclusions (documented in `docs/reviews/agents.md`, not
converted): daemon-shutdown cancel
(`_cancel_active_agent_runs_for_shutdown`,
`runner_lifecycle_agents.py:238-273`) is safe by a branch-complete
argument, not by shutdown-time persistence —
`_register_persisted_completion_subscribers`
(`runner_lifecycle_agents.py:16-33`) only *loads* previously persisted
`completion_subscribers` rows into the in-memory registry (it reads
`get_completion_subscribers` and calls `registry.register`; it writes
nothing), and the durable rows themselves were persisted earlier by
strict `wait_for_agent` registration (§1.1). Shutdown then has exactly
two branches, and both obey delete-after-acknowledgement: if
`terminalize_cancelled_run` performs the transition (`transitioned_here`
true, `agent_cleanup.py:481-489`), it notifies and — post-1.4 — delivers
through the helper to the loaded waiters, removing exactly the rows the
delivered map acknowledges (`ism_persisted` or `session_not_found`) and
retaining every failed delivery for startup replay; if the
capture-policy default terminalizer already transitioned the run during
`kill_agent(close_terminal=True)`, `transitioned_here` is false, no
shutdown-time notify or cleanup runs, and the untouched durable rows are
delivered by the next-boot acknowledged sweep (§1.3). No row is removed
without an acknowledged delivery, so a registered waiter is woken in
both branches — live at shutdown or by the sweep; startup missing-tmux
reconciliation (`_cleanup_missing_tmux_agent_run`,
`runner_lifecycle_agents.py:212-235`) already routes through
`cleanup_agent` — canonical; the expansion-run managers
(`storage/expansion_runs.py` and its expansion tools/CLI callers) write
`expansion_runs`, not `agent_runs`; and the kill CLI routes through the
daemon MCP tool (`cli/agents.py:595-643`).

Fix — step zero, terminal-transition atomicity at the storage root.
Every helper placement below relies on "terminalize returned ⇒ the
caller holds the transitioned run" and "terminalize raised ⇒ nothing
committed", and today the second half is false:
`LocalAgentRunManager.complete`/`fail`/`timeout`/`cancel` each commit
their terminal UPDATE through `HubDatabase.execute` — which opens and
commits its own transaction when no ambient one exists
(`storage/hub/postgres.py:375-385`) — and then run
`_expire_sessions_for_run_ids` and `get` as separate post-commit
statements (`storage/agents/_lifecycle.py:192-351`), either of which
can raise and leave a committed terminal row behind a propagating
exception that no producer converts into delivery. All four methods
first refuse ambient nesting: if `ambient_transaction(self.db)`
reports an active transaction for the adapter
(`storage/hub/_ambient.py:59-67`), the method raises a dedicated
`TerminalTransitionNestedError(RuntimeError)` (defined beside the
mixin) before executing the UPDATE — reentrant `enter_transaction`
merely yields the existing transaction with no savepoint and no
rollback-only marking (`_ambient.py:29-56`), so a nested wrap would
let an outer caller catch a post-UPDATE fault and still commit the
terminal UPDATE at outer exit, and on success would defer the commit
past the method's return, breaking both the atomic contract and the
1.2 commit-before-delivery invariant. The guard costs nothing today:
no swept producer calls the four methods inside an ambient
transaction (the only `db.transaction()` blocks in caller modules are
in unrelated functions — `build/controls.py:855`,
`_delete_child_session` in `spawn_agent/_failure_cleanup.py:126`, and
the bulk sweep UPDATE in
`src/gobby/storage/agents/_cleanup.py:127`). With
nesting excluded, each method wraps the terminal UPDATE, the session
expiry, and the row re-read in one `self.db.transaction()` block that
is always outermost, making it atomic from its caller's perspective:
it returns the transitioned run (re-read inside the transaction,
committed before the method returns), returns `None` on the
zero-rowcount already-terminal guard, or raises with the transition
rolled back and the run still pre-terminal, where the next producer
(sweep tick, retry, or boot sweep) terminalizes and delivers. The one residual shape — an
indeterminate commit outcome such as a connection drop during commit
acknowledgment — is the same class as a failed delivery: the durable
rows are retained and the next-boot sweep delivers (1.3).
`cleanup_stale_runs` additionally isolates its per-id loop: each
`self.timeout` call is wrapped in try/except that logs and continues,
so a raising id — which under atomicity transitioned nothing and is
re-matched next cycle — cannot abort the sweep and withhold earlier
transitioned ids from the returned list
(`cleanup_stale_pending_runs` needs no per-id isolation: its single
bulk UPDATE already commits atomically inside one transaction,
`storage/agents/_cleanup.py:119-161`). This atomicity underwrites
every placement below — the MCP kill routing's re-reads, the
AgentCleanup terminalizers' notify-after-return,
`complete_and_notify_agent_run`, the no-monitor fallbacks, and both
stale sweeps — none of which can now observe a committed-but-raised
transition from the storage methods themselves (1.4.15, 1.4.16). The
three outcomes are a same-thread contract, though: at an executor
offload — `asyncio.to_thread`, or the injected `DatabaseExecutor.run`
bridge that submits to a bounded pool and awaits the queued future
(`storage/executor.py:60-70`), the bridge production wiring hands the
lifecycle monitor and cleanup handler
(`runner_init/orchestration.py:181-200`) — the awaiting producer can
still observe `asyncio.CancelledError` while the worker runs the
storage call to completion and commits. Step zero therefore extends
across the offload boundary with a fourth guarantee, supplied by the
shielded delivery scope below, pinned at one boundary — scope entry:
a cancellation that lands before the producer invokes the scope
performs no transition work, and a cancellation that lands after the
scope owns the offload waits for the owned transition and its
acknowledged delivery or retention to settle before re-propagating —
even when the executor worker is still queued and unstarted at
cancellation time, since the owned task is never cancelled and,
while the daemon is serving, the executor runs every submitted call
to settlement (1.4.17). Caller-task cancellation is not the only
revocation threat, though: `asyncio.shield` is powerless against
executor-level future cancellation, and daemon shutdown explicitly
revokes queued unstarted work — `_shutdown_database_executor` calls
`db_executor.shutdown(wait=False, cancel_futures=True)`
(`runner_lifecycle_shutdown.py:542-552`) — after the graceful phase
has already cancelled live request tasks and stopped the monitor on
bounded waits (`runner_lifecycle_shutdown.py:211-226`, `:424-445`),
so without an ordering guarantee a shield-owned transition could
still sit queued when its underlying future is revoked. The scope
therefore pairs with a shutdown quiescence-and-drain sequence plus
an owned, cancellation-armored finalizer settlement sequence —
admission close, drain, barrier (below): inside
the shutdown deadline every in-flight owned scope settles before any
executor work is revoked, and past the deadline the finalizer
closes admission itself and re-drains owned scopes under its own
ten-second budget (below) — within that budget every owned scope
settles to commit, re-read, and acknowledged delivery, or bounded
retained-row resolution, with the database still open, while a
scope that stalls past the budget is logged, detached from the
tracked in-flight set, and abandoned — before the
executor barrier revokes or awaits anything (1.4.18–1.4.21).

Fix — the cancellation-shielded delivery scope. One primitive,
`shielded_terminal_delivery`, defined beside
`deliver_and_cleanup_terminal_run` (1.3) in `agent_cleanup.py`,
makes terminal transition plus delivery cancellation-safe: it runs
the transition offload and the follow-on terminal re-read plus
awaited `deliver_and_cleanup_terminal_run` inside a separately-owned
asyncio task, and the caller awaits that task through
`asyncio.shield`; when the caller's task is cancelled, the owned
task keeps running — the scope itself never revokes a submitted
offload, even while its executor worker is still queued and
unstarted, because revoking a queued worker races its start and
contradicts the keep-running semantics — the scope waits for its
settlement — re-entering the wait if further cancellations land
during shutdown — and then re-raises the original `CancelledError`,
so the caller-visible contract is: the await raised ⇒ the scope was
never entered and no transition work ran, or the owned
commit-and-delivery settled first. Every owned task the scope
starts is tracked in a module-level in-flight set beside the
primitive, discarded on settlement — the bookkeeping the shutdown
drain below consumes. Ordinary exceptions keep their
existing semantics — they propagate only after the owned task
settles — and a failure inside the scope after commit but before
acknowledged delivery leaves the durable rows retained for the
startup sweep (1.3). Routed producers:
`complete_and_notify_agent_run`, whose complete-run offload plus
helper await form one scope (1.4.8); the AgentCleanup terminalizers'
transition-plus-notify chains; both stale sweeps at every in-daemon
caller through one shared acknowledged-sweep operation,
`run_acknowledged_stale_sweeps`, whose owned scope holds the sweep
offload and the per-id helper loop together — the monitor
`_check_loop` sweep, the dispatcher heartbeat reap, the
cleanup-handler wrapper and its startup call, and the
`POST /api/agents/cleanup` route (1.4.11), whose HTTP request task
daemon shutdown cancels (`runner_lifecycle_shutdown.py:211-226`),
each invoke it, so a shutdown cancellation cannot sever transitioned
ids from their deliveries (1.4.9); and every direct
sync-transition-then-helper
placement below — the MCP kill routing, websocket observe-continue,
the HTTP cancel route, dispatch cleanup, the test-mode admin
endpoints, and the spawn/resume failure paths — whose re-read plus
helper chains run inside the scope so their exception wraps hold
under `BaseException` semantics: the re-read plus helper runs on
`CancelledError` exactly as on ordinary exceptions, settles, and the
cancellation then re-propagates. The capture chain gets the
transition half of the same guarantee at its own offload:
`_async_storage_call`'s terminalize call is awaited through an owned
settled task rather than the bare bridge (`capture.py:339-344`
today), so a caller cancellation mid-capture lets the capture-policy
terminal commit settle before `CancelledError` re-propagates up
through `_close_tmux_session` — whose handler catches only
`Exception` — to the kill surface, whose shielded re-read plus
helper then observes the committed row and delivers before
re-raising (1.4.17). One terminal producer stays outside the scope
by design: `SessionCoordinator`'s scheduled delivery (below) commits
its transition synchronously on the hook thread, which cannot await
the loop-side chain; it carries a retention-based next-boot
exclusion instead — every durable row survives any interruption of
the scheduled chain, and the first-operation startup sweep (1.3)
replays the retained rows on the next boot ahead of every fallible
optional startup step and regardless of monitor or pipeline-runtime
availability (1.4.6).

The drain that closes the executor-revocation gap — and the
quiescence boundary that makes it a closed set. A one-shot snapshot
of the in-flight set is not a drain: shutdown's graceful phase gives
remaining request tasks only a bounded cancellation poll
(`runner_lifecycle_shutdown.py:211-226`) and bounds the monitor stop
(`:424-445`), and one producer owner has no shutdown stop at all
today — `schedule_tmux_health_check` parks free-running tasks in the
module-level `_health_check_tasks` set (`_health.py:118-139`), whose
`cancel_health_checks` (`:38-42`) cancels without awaiting and has
no production caller, so `_deferred_tmux_health_check` can invoke
its terminal scope after any snapshot. Three pieces close the set.
First, producer quiescence: the graceful phase cancels and awaits
the deferred health-check tasks through an awaited quiescence
companion beside `cancel_health_checks`, bounded like the
neighboring stops; a health task cancelled mid-scope settles its
owned transition first — scope semantics — and its own
`except asyncio.CancelledError` handler then swallows the exit
(`_health.py:112-113`). Second, an admission boundary: a
module-level flag beside the in-flight set, closed idempotently in
two places — by `_run_async_shutdown_cleanup`
(`runner_lifecycle_shutdown.py:659-684`) before the in-block drain,
and again by the finalizer as its first settlement step, because
the overall deadline wraps async cleanup and can expire before it
starts, so the finalizer can never assume a closed boundary and
closes it itself. The close is race-free by construction: the flag
write and every scope's check-then-enter admission section are
synchronous sections on the loop thread, so no producer sits
between the flag check and its in-flight entry when the close
lands. The flag's lifecycle is close-and-reopen, not close-only:
`run_daemon` supports embedded and test callers and returns
without terminating the interpreter
(`runner_lifecycle.py:111-143`), so a second daemon lifecycle in
the same interpreter would otherwise inherit a closed boundary
and every delivery scope would refuse transition work forever.
The reopen — and every other process-global lifecycle
mutation — is sited after ownership. Round 29 falsified a
construction-time assertion-and-reopen; round 30 falsified
the post-claim activation step as a sufficient boundary on
its own, because `GobbyRunner` construction is itself
process-global: `__init__` runs its init blocks
unconditionally (`runner.py:183-195`), and those blocks
already write daemon-wide state — file logging and the
telemetry providers (`runner_init/storage.py:70,148,192`),
the daemon-wide tmux helpers
(`runner_init/orchestration.py:136`,
`agents/tmux/__init__.py:59-67`), and the global
`ServiceContainer` plus module broadcast callbacks
(`runner_init/servers.py:79,163`,
`app_context.py:267-276`) — and open real resources, the
hub pool and the managed `DatabaseExecutor`
(`runner_init/storage.py:106-112`), while the embedded
entry constructs before any claim exists
(`runner.py:212-218`; `runner_lifecycle.py:157-175`) and
`run_daemon` writes `_startup_tracker` and installs signal
handlers ahead of its embedded claim
(`runner_lifecycle.py:147-165`;
`runner_maintenance.py:928-981`) and clears the global app
context in its unconditional terminal `finally`
(`runner_lifecycle.py:288`). A contending runner B reaching
any of that would overwrite or clear serving daemon A's app
context, signal routing, tmux and logging state, and leak
B's pool and executor threads before B ever discovered it
lost. The boundary therefore sits ahead of construction:
ownership precedes `GobbyRunner` construction in every
supported entry shape. The standalone shape already claims
first (`main()`, `runner.py:297-315`); the embedded shape
moves the claim into `run_gobby`, ahead of
`GobbyRunner(...)` — a contended claim logs and returns
with no runner constructed, nothing written, nothing
opened, and a claim failing with `OSError` keeps today's
fail-open-unlocked semantic, resolved before construction —
and `run_daemon`'s own embedded claim block is deleted.
Round 31 falsified deletion alone as the enforcement:
`GobbyRunner.run` and `run_daemon` both default
`pid_claim=None` (`runner.py:197-200`;
`runner_lifecycle.py:111`), and bare `runner.run()` and
`run_daemon(runner)` calls are live across
`tests/test_runner_lifecycle.py`,
`tests/test_runner_pid_file.py`, and
`tests/test_runner_shutdown.py`, so after deletion either
call would reach the `_startup_tracker` write, the
signal-handler installation, and the terminal
app-context-clearing `finally` with nothing resolved.
Resolved ownership is therefore an explicit value — a held
`PidFileClaim`, or a distinct fail-open-unlocked
resolution produced only by the `OSError` branch;
contention is never represented, because a contended entry
exits before construction — and a required, non-default
parameter of both `GobbyRunner.run` and `run_daemon`: the
`None` defaults and their bypass are removed, calling
without a resolution is a `TypeError`, and every direct
caller resolves ownership before `GobbyRunner`
construction. Because `run_gobby` acquires the embedded
claim itself, it also owns the failure path: construction
runs four fallible init blocks with no rollback of their
own (`runner.py:183-195`) — the missing-config
`FileNotFoundError` (`runner_init/storage.py:60-65`)
raises before any resource exists, but later stages fail
with resources live — and on any construction raise
`run_daemon` never starts, its release helper
(`runner_lifecycle.py:138-143`) is unreachable, and
`main()`'s terminal release (`runner.py:321-325`) covers
only claims `main()` owns. Round 32 falsified bare
release as that failure path: completed stages own real
resources and process-globals — the hub pool and
`DatabaseExecutor` (`runner_init/storage.py:106-112`), the
app context published before the Codex, web-chat, HTTP,
and WebSocket constructors run
(`runner_init/servers.py:79-112`), the span-exporter
registration on the global tracer provider, latched for
the interpreter and holding the lifecycle's `SpanStorage`
when traces are enabled (`telemetry/providers.py:54-69`) —
so releasing the claim after a mid-construction raise
frees the singleton lock while the continuing interpreter
still owns a partial pool, live executor threads, and
mutated globals, and a successor can acquire the freed
lock and construct over the leftovers. Round 33 then
falsified the hand-maintained reverse-order inventory
itself: the services stage builds the memory stack — a
`VectorStore` and a `MemoryManager` whose constructor
creates a Falkor client
(`runner_init/services.py:117-173`,
`memory/manager.py:66-193`) and whose teardown is
asynchronous (`memory/manager.py:309-316`), awaited with a
five-second bound by normal shutdown
(`runner_lifecycle_shutdown.py:523-539`) — the
orchestration stage installs the tmux module helpers
(`runner_init/orchestration.py:136`,
`agents/tmux/__init__.py:59-67`), and `HTTPServer`
construction publishes the tool-summarizer module globals
(`servers/http.py:237-239`,
`utils/tool_summarizer.py:25-35`), none of which a fixed
list covering callbacks, app context, telemetry, executor,
and hub pool restores; and `shutdown_providers` is no
rollback at all, because `init_telemetry`
(`runner_init/storage.py:148`) installs the tracer and
meter providers through one-shot OpenTelemetry API setters
(`telemetry/__init__.py:95-101`) that cannot be unset in
the interpreter, so shutting the cached providers strands
live instrumentation on a dead provider and a successor's
exporter on a shadow one. Construction failure is
therefore transactional through a construction rollback
ledger, not a hand-maintained list: each init stage
appends an undo entry immediately after each
rollback-relevant install — the hub database, the
`DatabaseExecutor`, the lifecycle-owned telemetry
attachments, the memory stack, the tmux helpers, the
published app context, the summarizer globals, and the
agent-event and pty/tmux output callbacks
(`runner_broadcasting.py:113-231`) — and on a failing
stage `GobbyRunner.__init__` unwinds the ledger
last-in-first-out, driving the asynchronous entries
(`MemoryManager.close`, `VectorStore.close`) with
`asyncio.run` under the same five-second bounds normal
shutdown uses (no event loop runs during construction),
re-raising only after the unwind completes, so the
exception `run_gobby` observes already implies a clean
interpreter. Telemetry rolls back by ownership: the
OpenTelemetry API providers and the LLM instrumentors are
interpreter-latched — installed once, never rolled back,
with provider acquisition reusing an already-installed
API-global provider rather than constructing a shadow the
one-shot setter rejects — while the lifecycle-owned
`SpanStorage` span processor is shut down with its latch
reset and health metrics are disabled via
`configure_health_metrics(enabled=False)`. `run_gobby`
then releases any claim it acquired, idempotently, when
construction or startup raises, leaving
the lock free for the next lifecycle. The `_startup_tracker` write, the
signal-handler installation, and the terminal
app-context-clearing `finally` thus execute only in a
lifecycle that holds the pid file or is the sole fail-open
unlocked runner — by signature, not convention. Within that
owned lifecycle, `run_daemon` runs the
lifecycle-activation step (1.4.20) after its boot gate
(1.4.21) has completed, still before the Uvicorn server
object exists and before any producer or HTTP service can
run: the step asserts the prior lifecycle's in-flight set
is empty — an assertion both predecessor exits satisfy,
since settlement empties the set within the finalizer
budget and the expiry branch's detach empties it at
abandonment, and one no contender can reach, because a
contended entry returns before construction and mutates
nothing — then resets the flag open and installs the module
seams below. A scope invocation
after the close performs no
transition work at all — no owned task, no executor submission, one
log line — and its run stays pre-terminal with every durable row
retained, resolving through the existing daemon-down two-stage chain
(the next boot's stale sweeps terminalize it and acknowledged
delivery follows, 1.4.9, 1.3). The designed shutdown-cancel producer
(`_cancel_active_agent_runs_for_shutdown`) runs in the graceful
phase, before the boundary, and is untouched. Third, the drain
itself: `drain_shielded_terminal_deliveries`, defined beside the
primitive, loops snapshot → await → re-check until the in-flight set
is stably empty — termination is guaranteed because admission is
closed and producers are quiesced, so the set only shrinks — and
nothing can enter after the boundary, so there is no awaitable gap
between the final empty check and what follows.

The barrier below governs only the managed `DatabaseExecutor`, so
every scope-owned terminal offload must actually run there — and
today two of them do not. The complete-run offload inside
`complete_and_notify_agent_run` rides asyncio's loop-default
executor through a bare `asyncio.to_thread` bridge
(`run_completion.py:27-31`), and capture's `_async_storage_call` is
that same bare bridge (`capture.py:339-344`), while the cleanup
handler's and monitor's offloads already run managed — their
injectable `run_db` defaults (`agent_cleanup.py:97-100`,
`lifecycle_monitor.py:216-219`) receive the managed executor's
`run` at init (`runner_init/orchestration.py:100`) — and
`_shutdown_database_executor` reaches only the custom executor's
own pool (`storage/executor.py:105-111`). Left alone,
default-executor transition work would survive the finalizer
untouched and race `runner.database.close()`. The fix generalizes
the existing seam once instead of threading a parameter through
every caller: a module-level terminal-offload seam lives beside the
`shielded_terminal_delivery` primitive in `agent_cleanup.py`,
defaulting to the bare bridge, and the post-claim activation step
(1.4.20) points it at the managed executor's `run` — the
instance-local `run_db` defaults keep their construction-time
supply (`runner_init/orchestration.py:100`), but no module-level
seam is written before the pid claim is held;
`complete_and_notify_agent_run`'s offload and every
`_async_storage_call` site route through the seam, so tests keep
today's defaults while production offloads become
executor-governed with no caller signature changes. The
coordinator's hook-thread terminal commit joins the same choke
point through the synchronous door, and that door is the module
seam too — not constructor threading, because the construction
chain has no executor anywhere: `SessionCoordinator.__init__`
accepts none (`hooks/session_coordinator.py:88-132`), and none is
supplied by `HookManagerFactory.create`, by `HookManager`'s
factory call, or by the HTTP app-lifecycle wiring that constructs
the hook stack — threading one through would drag that whole
wiring chain into scope for a single call site. Instead `DatabaseExecutor` gains a sync
`submit` counterpart to `run` (`storage/executor.py`) returning
the `concurrent.futures.Future`, the seam module beside
`shielded_terminal_delivery` gains a matching synchronous
seam function — defaulting to inline invocation on the calling
thread when unset, pointed at the managed executor's `submit` by
the same post-claim activation step (1.4.20) —
and `_terminate_agent_run`'s storage call goes through that
module-level function and blocks on the returned future. None of
the four wiring files above is touched. The failure taxonomy is
named exactly: a queued submission revoked by the barrier's
`cancel_futures` raises `concurrent.futures.CancelledError` from
`Future.result()` on the hook thread — in CPython 3.14 that is a
distinct class inheriting `Exception`
(`concurrent.futures._base.CancelledError` → `Error` →
`Exception`), so the coordinator catches it by name ahead of its
broad handler rather than relying on it escaping — and a
submission after shutdown raises `RuntimeError` from the
executor's shutdown guard (`storage/executor.py:105-111` plus the
`run`-side guard at `:60-70`), caught the same way. Both shapes
commit nothing: the run stays active, every durable row is
retained, and the next boot's stale sweeps terminalize and deliver
it through the existing daemon-down chain (1.4.9, 1.3). The
invariant the finalizer then rests on: no terminal-transition
storage write in the daemon process executes outside the managed
executor.

Executor finalization is a close-then-drain-then-barrier sequence
in the finalizer — unconditional, not deadline-dependent. The in-block
drain runs inside `shutdown_daemon_services`' existing overall
`asyncio.timeout_at` deadline, and that is exactly why
`_run_async_shutdown_cleanup` cannot own executor settlement: on
deadline expiry before cleanup starts, or on the deadline landing
mid-drain, everything inside the timeout block is skipped or
cancelled while the outer finalizer closes the database — executor
transition work would outlive `runner.database.close()` with no
settlement at all. A synchronous blocking worker join on the
loop thread cannot be the answer either: `DatabaseExecutor.run`
resolves through loop-owned futures (`loop.run_in_executor`,
`storage/executor.py:60-70`), so blocking the event-loop thread on
the executor barrier starves the very completion callbacks the
owned scopes need — workers finish, but their asyncio owners
cannot settle, and the follow-on terminal re-read and acknowledged
delivery would land only after the database closes, or never. The
finalizer therefore settles in three ordered steps, all with the
loop live, and the whole settlement — steps, database close, and
pid cleanup — runs as one owned task armored against caller
cancellation: `run_daemon` awaits `shutdown_daemon_services`
directly (`runner_lifecycle.py:258`) and embedded and test
callers cancel that await, so a bare await inside the finalizer
would take the cancellation mid-drain or mid-barrier and either
skip the database close outright or close it under live delivery
work; instead the finalizer spawns the settlement sequence as its
own task and awaits it through a shield-and-rewait loop that
absorbs every delivered cancellation — a second or later
cancellation lands at the shielded await and re-enters the loop —
then re-raises the original cancellation only after the database
close and pid cleanup inside the owned task have settled. Step
zero closes admission idempotently, as above, so the
deadline-expiry-before-cleanup shape drains behind a closed
boundary too. Step one re-runs `drain_shielded_terminal_deliveries`
— the same stable-empty loop 1.4.18 defines, idempotent because
its owned tasks are shielded and survive the timeout cancellation
— under the finalizer budget: within it, every queued or started
scope-owned
transition runs its offload, commit, terminal re-read, and awaited
acknowledged delivery while the database is open, and the
in-flight set empties by settlement; a transaction stalled in a
client-unbounded shape past the budget takes the expiry branch
below — logged, detached, abandoned — and the set empties by
detachment instead. Termination is enforced by closed
admission, by per-operation bounds spanning the settlement
chain, and — because those bounds are server-enforced `SET LOCAL`
statements that cannot cover their own installation or the
transaction machinery around them — by the client-side finalizer
deadline below, never by statement arithmetic alone: the terminal UPDATE is one
short statement inside one transaction block (1.4.15's step-zero
atomicity) under the transaction-scoped bounds below (1.4.21); the
shielded terminal re-read goes through a bounded read defined
beside the fence constants (1.4.21); acknowledged delivery's
database work is bounded at its call sites through the shared
bounded-transaction helper (1.4.21) and executes off the
event-loop thread — the round-22 walk of `WakeDispatcher.wake`'s
real chain found three loop-thread reads the round-21 statement
list missed, and database-layer bounds cap their ordinary
statements, but `CompletionEventRegistry.notify` awaits the wake
callback on the registry-owning loop
(`events/completion_registry.py:83-124`) and `wake` reached its
session lookup and `_send_ism` synchronously before its next
await (`events/wake.py:111-142`), so a delivery transaction
stalled in a client-unbounded shape — `BEGIN`, the first
`SET LOCAL`, or COMMIT/ROLLBACK — would have wedged the loop
thread itself, and a wedged loop runs no timers: the finalizer
watchdog below could never fire, the expiry detach could never
run, and database close and pid release would never execute.
Every delivery-path database transaction covered by the
finalizer proof therefore runs through the managed executor
offload on a worker thread, awaited from the loop, never as a
synchronous call on the loop thread: `wake`'s initial session
lookup (`events/wake.py:111-142`, today an unbounded loop-thread
point read through `storage/sessions/_identity_crud.py:56-61`)
becomes one offloaded helper-wrapped read; `_send_ism`
(`events/wake.py:498-529`) runs its dedup read and message
insert in one offloaded helper-owned bounded transaction, whose
`SET LOCAL` scope the manager statements join ambiently — the
message manager itself is untouched, and its other
ambient-transaction callers (the mailbox send path,
`sessions/mailbox.py:157-174`) keep their current unbounded
loop-side semantics because the bounds and the offload live in
the wrapper, never in the manager; the subscriber-row removal is
wrapped and offloaded the same way at its delivery call site in
the cleanup helper; the dispatcher receives the managed executor
as instance-local constructor wiring at daemon init (1.4.20) —
state on this runner's own objects, touching no module global,
and construction itself is post-claim in every entry shape
(1.4.20), so no contending construction exists to race it — and a
scope-owned delivery submits these offloads
against the executor handle its scope captured at entry, so the
expiry branch's abandonment covers a wedged delivery worker
exactly as it covers a wedged transition worker. A bound expiry
in the dedup read propagates to `_send_ism`'s broad handler
rather than vanishing into the unbounded fallback listing —
`_notification_exists`'s intermediate handlers re-raise the
bound-expiry error class, and the fallback listing it still runs
for managers without the dedup method joins the same offloaded
ambient bounds (`events/wake.py:531-583`);
the best-effort live-nudge phase after durable ISM persistence —
the per-session wake lock and the tmux, web-chat, and SDK
callbacks (`events/wake.py:144-156`) — is bounded by a
client-side deadline in `WakeDispatcher.wake` for its awaited
callbacks, while its residual reads are offloaded and bounded
like the rest: SDK resolution
(`events/wake.py:473-496`) reuses the session `wake` already
loaded when it is present, and any residual session or agent-runs
lookup runs under the helper's bounds through the offload with
the existing best-effort catch intact, so a bound expiry there
degrades the nudge, never the stored notification, and the
per-session lock always releases for the next wake; and
connection acquisition and reconnects are bounded by the pool's
client timeout plus the connection kwargs below (1.4.21). A
delivery statement that overruns its bound raises into
`_send_ism`'s existing broad handler,
the wake reports durable failure, and the scope's
retention branch keeps every durable row for
next-boot delivery — the drain still reaches stable empty, with
that scope settled in the retained-row state rather than holding
the barrier open; a delivery transaction stalled in a
client-unbounded shape wedges only its worker thread — the loop
keeps serving timers, the watchdog expires, and the expiry
branch below abandons the scope. Step two, the
executor barrier — split into a synchronous close-and-revoke and
a separate worker join, because `ThreadPoolExecutor`'s join is
unkillable from the client side and revocation must never depend
on which thread wins a scheduling race. Round 29 falsified the
round-28 guard argument: today `DatabaseExecutor.shutdown`
writes its `_shutdown` flag under the executor lock but calls
the underlying pool shutdown only after releasing it
(`storage/executor.py:105-111`), so a barrier thread that set
the flag and was descheduled before the underlying call would
make a concurrent expiry-branch call hit the guard and return as
a no-op with nothing actually revoked — the finalizer would
close the database and release the pid while queued futures
still sat startable in the pool. The shutdown protocol therefore
separates the two concerns in `DatabaseExecutor` itself:
`shutdown` becomes the non-blocking close-and-revoke whose flag
write and underlying
`ThreadPoolExecutor.shutdown(wait=False, cancel_futures=True)`
execute together inside the executor lock — the underlying call
is non-blocking, so holding the lock across it is bounded, and
any caller that observes the guard set knows revocation has
already completed, by lock atomicity rather than by thread
scheduling — and a new guard-free `join` method performs only
the blocking worker join. The finalizer runs close-and-revoke
synchronously from the loop on both branches, as step two's
first act ahead of `runner.database.close()`: the flag write and
queue drain wedge on nothing, so the loop-thread call is safe,
and revocation never rides another thread. On the within-budget
branch the worker join then runs on a dedicated Gobby-owned
daemon barrier thread spawned by the finalizer, completion
signaled back to the loop with
`call_soon_threadsafe` and awaited under the remaining finalizer
budget — never through `asyncio.to_thread`: the existing
wait=False comment (`runner_lifecycle_shutdown.py:545-549`)
warns that a timed-out `to_thread` call keeps running and
`asyncio.run` waits on that stranded default-executor worker
again at loop close, so a `to_thread` barrier that missed the
deadline would either defeat the deadline or hang loop teardown,
while an abandoned daemon barrier thread blocks neither. A join
that completes inside the budget means every started worker
finished; queued work was already revoked by the synchronous
step, the in-flight
set is already empty, so no scope-owned terminal transition can
be revoked here, and the only terminal submissions the revoke
can catch are the coordinator's sync submits, which fail closed
per the named taxonomy above. On the expiry branch — the
deadline landing mid-drain before the barrier, or the join
await timing out — the finalizer never waits on the join: in
the mid-drain shape it runs the synchronous close-and-revoke
at expiry, after detaching the abandoned scopes, and in the
join-timeout shape close-and-revoke already ran ahead of the
join — either way revocation is complete on the loop before
the expiry branch proceeds to database close, and the
barrier thread, if one was spawned, is at worst a leaked daemon
thread blocked in
the join. Started submissions are never awaited on the expiry
branch — they are accounted through tracked ownership instead:
scope-owned work is in the in-flight set, settled within the
budget or logged, detached, and abandoned by the expiry branch;
coordinator sync submits block their own hook thread on
`Future.result()`, never the finalizer; and residual
non-terminal work runs to completion or failure on its worker
against a closing pool, which psycopg_pool tolerates —
checked-out connections stay open until returned, and any new
acquisition raises `PoolClosed` and commits nothing. The
`_shutdown_database_executor`
call (`runner_lifecycle_shutdown.py:542-552` today) relocates into
this finalizer sequence as that branch-invariant synchronous
close-and-revoke, immediately ahead of
`runner.database.close()` (`runner_lifecycle_shutdown.py:762-766`);
the ordering guarantee is branch-invariant for revocation and
branch-qualified only for the join: admission close and the
synchronous close-and-revoke's queued-work revocation precede
database close on every path, the completed worker join
additionally precedes close on the within-budget branch, and
the expiry branch proceeds to close without awaiting the join.
Round 26 closed the finalizer's own unbounded shapes: the re-drain
and the barrier live in the `finally` block that already runs
close and pid cleanup (`runner_lifecycle_shutdown.py:762-773`),
outside both asyncio deadlines that bound the try body, and a
settlement transaction is client-unbounded against a server that
accepts connections but stalls execution in three shapes — the
transaction entry's `BEGIN` and its COMMIT-or-ROLLBACK teardown
(`storage/hub/postgres.py:349-373`), and the first `SET LOCAL`,
which no timeout can bound because it is the statement that
installs the timeout — while connection establishment and pool
acquisition stay client-bounded by `connect_timeout` and the
pool's bounded two-attempt window, and the wedged worker thread
stays awaited through `DatabaseExecutor.run`
(`storage/executor.py:60-70`) and cannot be killed. The finalizer
therefore runs the re-drain and the barrier under one declared
client-side finalizer deadline — ten seconds on a monotonic
clock, measured at finalizer entry, a constant defined beside the
finalizer — with the barrier's dedicated-thread join awaited
within whatever the drain leaves. When every statement bound holds, settlement
completes well inside it and nothing changes. When a settlement
transaction stalls in a client-unbounded shape, the deadline
expires: the finalizer logs each abandoned scope by run id — no
silent truncation — and detaches it, discarding the abandoned
owner's entry from the tracked in-flight set with an idempotent
discard the task's own eventual finalizer re-runs as a no-op, so
the expiry branch leaves the set as empty as settlement does and
the successor's activation-time empty-set assertion (1.4.20) holds
unconditionally; the abandoned owner holds only
predecessor-lifecycle handles captured at scope entry — its
executor, database, and registry — so a post-severance unwind
exercises predecessor objects alone, against a closed pool and an
already-abandoned future, and never touches a successor seam.
Abandoned scopes' durable rows follow the
delete-only-after-acknowledged-delivery invariant, not universal
retention: a scope abandoned before acknowledged delivery retains
its rows because nothing authorized removal, while a scope
abandoned during its post-acknowledgement row-removal COMMIT may
leave the rows present or absent — both safe, for reasons split
by the acknowledgement's classifier branch. An `ism_persisted`
acknowledgement stored the durable notification before removal
was issued, so a surviving row redelivers idempotently through
the ISM dedup and an absent row needs no redelivery. A
`session_not_found` acknowledgement stored nothing — the wake
returns that code before `_send_ism` runs
(`events/wake.py:111-128`) — and its safety rests on permanent
session absence instead: a deleted session can never be woken
and never consumes a row, so an absent row lost nothing, and a
surviving row is re-attempted by the next boot's sweep,
classified `session_not_found` again, and removed. The finalizer
then proceeds to database close, pid release, and the original
outcome in the pinned order. Abandonment is safe against
predecessor-writes-after-ownership-transfer on four grounds.
First, an abandoned transaction that had already taken its shared
fence acquisition blocks the successor gate's exclusive
acquisition until it resolves, so its commit lands before the
successor's post-fence snapshot — the pinned indeterminate-COMMIT
interleaving. Second, an abandoned transaction that had not yet
reached its fence acquisition can never commit after ownership
transfers, because the successor's gate severs it: the severance
sweep below terminates every surviving predecessor backend while
the exclusive lock is held, and the predecessor's closed pool
(`PoolClosed` on any new acquisition) means no replacement
backend can ever appear. Third, even a late transition landing
while no owner exists at all is just a daemon-down terminal — the
zero-rowcount already-terminal guard (1.4.15) makes it a no-op if
the row was meanwhile terminalized, and otherwise the next boot's
post-fence snapshot observes the committed row and its sweep
delivers the retained rows. Fourth, whatever rows abandoned scopes retain
resolve on the next boot's acknowledged sweep, the
same daemon-down-terminal path already specified — an
`ism_persisted` row redelivers through the dedup, a
`session_not_found` row is reclassified and removed — and a row
absent under the invariant above was removed only after an
acknowledgement under one of those two branches, so nothing is
lost either way. The leaked
worker threads self-heal on the same severance: a terminated
backend fails the wedged statement, the worker unwinds with a
connection error into its already-abandoned future, and every
pending join — the leaked barrier thread's and the interpreter's
atexit join alike — completes. Until severance, the executor's
non-daemon workers pin interpreter teardown, so process exit is
accounted per shape. Pid release lands in the pinned order
before any interpreter-exit join, so a successor's gate is
reachable by a fresh process no matter how wedged the old one
is. In the CLI-supervised shape the documented 20-second
force-kill (`storage/hub/postgres.py:483-491`) truncates a stuck
teardown externally, exactly as today. The standalone
entrypoint — `main()`'s `asyncio.run` call (`runner.py:315`)
under SIGTERM, KeyboardInterrupt, or SystemExit with no
supervisor — gains an expiry-branch-only exit backstop: the
finalizer records that it took the expiry branch, and after
`asyncio.run` returns with the pinned order complete — database
closed, pid released, outcome logged — and the entrypoint's own
idempotent pid-release finally has run, `main` exits through
`os._exit` with the status it would otherwise have returned,
forfeiting only the join of workers wedged in statements that
can never complete client-side. The embedded host, whose process
Gobby must never exit, gets `run_daemon` returning normally with
the pinned order complete and the leaked workers documented:
they unwind when any successor gate's severance fails their
statements, and until then the host owns its own process-exit
policy — which is why the finalizer deadline is load-bearing
precisely where no force-kill exists, the embedded host.
The pool-close argument rests on psycopg_pool's actual semantics:
`ConnectionPool.close` fails new acquisitions immediately but
explicitly leaves checked-out connections open until returned
(`psycopg_pool/pool.py:427-437`), so close alone bounds nothing —
on the within-budget branch the settled drain and the completed
worker join ahead of close are what guarantee no terminal
worker holds a connection when close runs, on the expiry branch
an abandoned worker may still hold one — which close leaves
open, and whose eventual commit or unwind the four
abandonment-safety grounds above cover — and any post-close
acquisition attempt raises `PoolClosed` and commits nothing. The
process-level backstop is the CLI force-kill documented at the
pool-close site (`storage/hub/postgres.py:483-491` — a 17-second
async deadline plus a three-second tail before the kill), and a
worker that wedges past that tail is where honesty matters: a
force-killed process whose backend had not yet received COMMIT is
rolled back server-side, but psycopg's transaction context sends
COMMIT on clean block exit (`storage/hub/postgres.py:349-373`),
and PostgreSQL can complete a COMMIT whose client died after
sending it — the outcome is indeterminate from the client side.
`claim_pid_file`'s OS lock plus dead-pid check
(`runner_pid_file.py:99-138`) proves the process stopped, not that
its last commit resolved, which is exactly why recovery carries
the database-visible predecessor fence below (1.4.21). The
finalizer ordering — drain, the synchronous close-and-revoke
with the worker join awaited only within budget, then
`runner.database.close()`, then `cleanup_pid_file` releasing the
singleton claim (`runner_lifecycle.py:138-143`,
`runner_lifecycle_shutdown.py:762-773`) — combined with the
choke-point invariant above yields: on every exit shape short of
process kill, a terminal-transition commit from this process
lands after ownership release only from an expiry-branch
abandoned scope, whose interleavings the four grounds above
pin — fence-held commits resolve before the successor's
post-fence snapshot, unfenced ones are severed by the successor
gate or land as daemon-down terminals; and on process kill, the fence
makes the replacement daemon's boot gate wait out any in-flight
predecessor commit before its first-operation recovery (1.3)
takes its snapshot, so no durable row is stranded either way.

The predecessor fence and the operation bounds close the two gaps
process-level reasoning cannot. Bounds first, issued through one
shared bounded-transaction helper on the hub
(`storage/hub/postgres.py`) whose first statements are
`SET LOCAL statement_timeout` and `SET LOCAL lock_timeout` —
scoped, so no global operation inherits them. Five terminal-status
transactions adopt it: the four lifecycle transitions (1.4.15) and
the bulk stale-pending transaction of `cleanup_stale_pending_runs`
(`storage/agents/_cleanup.py:119-161`), which writes
`status = 'error'` directly and would otherwise bypass every bound
and the fence below. A source sweep confirms these five are the
only terminal-status writers: `cleanup_stale_runs` terminalizes
through the fenced `timeout` method
(`storage/agents/_cleanup.py:29-117`), and the remaining
`agent_runs` UPDATE sites — the runtime mixin's field writes, the
termination mixin's intent and capture-slot writes, the messaging
result write, and the spawn-failure child-session clear — never
write `status`. The delivery-path database work named above — the
wake session lookup, the dedup read and message insert, the SDK
lookup, the subscriber-row removal, and the bounded terminal
re-read — runs under the same helper through the call-site
wrappers above, which keep the storage managers themselves and
their ambient-transaction callers untouched. The hub pool's connection kwargs
gain `connect_timeout` (10 seconds), TCP keepalive settings
(`keepalives=1`, `keepalives_idle=30`, `keepalives_interval=10`,
`keepalives_count=3`), and a lifecycle-scoped `application_name`
marker — the literal prefix `gobby-hub-` plus a nonce minted at
each pool open, so a successor lifecycle in the same interpreter
carries a fresh marker — which the boot gate's severance sweep
below matches. The timeout and keepalive kwargs bound connection
establishment and dead-peer detection — and only those:
`connect_timeout` covers establishment alone, and keepalive
detects a dead peer, not a live server that accepts TCP while a
query stalls. The
post-`PoolTimeout` validation pass is therefore not bounded by
kwargs at all — `ConnectionPool.check()` runs `conn.execute("")`
per pooled connection with no deadline
(`psycopg_pool/pool.py:513-566`) — so the acquisition-retry path
in `_pool_connection` (`storage/hub/postgres.py:262-289`) sheds
the synchronous `pool.check()` and retries as a second bounded
acquire attempt under the existing 5-second client timeout
(`config/postgres_pool.py:9-16`), leaving broken-connection
disposal to the pool's own discard-and-replace machinery. COMMIT
itself carries no statement bound; its indeterminacy is resolved
by the fence, not by a timer. The fence: every terminal-status
transaction's first statements, after the bounds, include
`pg_advisory_xact_lock_shared` on a dedicated
fence key constant defined beside the terminal methods
(`storage/agents/_lifecycle.py`) — shared mode, so concurrent
terminal commits never serialize against each other, and
transaction-scoped, so the lock releases exactly when the
transaction resolves, commit or abort, including a commit whose
client was already dead. The exclusive counterpart is a boot gate
in `run_daemon` itself (`runner_lifecycle.py`), awaited
immediately after the pid claim and before the Uvicorn server
object exists. Placement is load-bearing, and round 22 falsified
the round-21 siting: subsystem initialization — where the first
recovery operation lives — is a background task that `run_daemon`
starts only after HTTP is already serving, and its completion
callback only logs a failure (`runner_lifecycle.py:111-232`), so
a fence gated inside the recovery module would exhaust its budget
with traffic already accepted and this daemon's own shared-mode
terminal writers already reachable through live requests; gated
ahead of the server, no request handler, subsystem task, or
shared-mode writer of this process exists yet, and the fence
resolves before any request can invoke a terminal writer. The
gate awaits an acquisition helper whose home is
`runner_lifecycle_agents.py`, beside the recovery sweeps it
fences: the helper takes the exclusive lock on the same key under
a single end-to-end gate deadline — sixty seconds on a monotonic
clock, measured at gate entry — then releases it. The deadline is
enforced structurally, not derived from statement arithmetic,
for two reasons round 25 made inseparable. First, no
per-statement bound can cover the whole gate under a server that
accepts connections but stalls execution: the `SET LOCAL`
statements themselves (a timeout cannot bound the statement that
installs it), a lock or diagnostic query whose server-side
`lock_timeout`/`statement_timeout` never fires because the
backend is wedged, and transaction teardown — COMMIT or ROLLBACK
— which carries no statement bound. Second, the enforcement
mechanism must survive the embedded lifecycle contract above:
`run_daemon` is a directly awaitable API that can return without
terminating the interpreter (`runner_lifecycle.py:111-115`), its
fatal handler raises `SystemExit` only from the
`except Exception` arm (`runner_lifecycle.py:283-288`) — which
an embedded host may catch — and caller cancellation bypasses
that handler entirely, so no argument from process exit can
retire a stuck in-process worker, and a Python thread cannot be
killed. Enforcement therefore places the gate body — connection
establishment, every attempt, backoff, and the diagnostic — in a
terminable child process: a minimal entry module
(`src/gobby/runner_gate.py`, importing psycopg and stdlib only)
that the helper launches as `sys.executable -m gobby.runner_gate`
via `asyncio.create_subprocess_exec`, handing it the DSN, fence
key, remaining budget, and this lifecycle's hub
`application_name` marker as one JSON document on stdin — never
argv or environment — and reading one JSON result from stdout.
The child's own connection carries a distinct `gobby-gate-`
marker the severance-sweep filter below never matches.
The child holds a single dedicated direct psycopg connection,
opened under the ten-second `connect_timeout` with the keepalive
settings above; it never touches the hub pool, so pool
starvation cannot slow the gate and pool close never waits on
it. The helper awaits the child under `asyncio.wait_for` with
the remaining deadline inside a `try/finally` whose finally
kills (SIGKILL) and reaps the child — a bounded reap, since a
killed process exits promptly — before any unwind continues, so
on watchdog expiry, on gate failure, and on caller cancellation
alike, the child is dead and settled before database close and
pid release run. Admission control remains the scheduling policy
inside the child: up to five lock attempts, each under a
five-second `SET lock_timeout` with one-second backoff between
attempts, and a step — establishment, attempt, backoff, or
diagnostic — starts only if the remaining budget covers its
bounded admission cost (ten seconds for establishment under
`connect_timeout`, five seconds of lock wait per attempt, five
seconds for the diagnostic — the costs client and server
timeouts do bound), so on every path where those bounds hold the
gate resolves without the watchdog firing. When they do not
hold, the deadline fires anyway: the boot side stops waiting at
expiry, kills and reaps the child, declares gate failure, and
proceeds to the fatal abort with the fallback message recording
the deadline expiry. The child's death severs its socket, and
the server resolves the severed in-flight fenced transaction by
whichever outcome applies — severance before COMMIT rolls it
back, while a COMMIT the server had already accepted may
complete after the client dies, exactly the indeterminacy the
shutdown analysis above already grants — and both outcomes are
safe because no gate statement writes a row: an attempt
transaction holds only the two `SET LOCAL`s and the exclusive
advisory-lock acquisition, and the diagnostic is a read-only
join, so rollback discards nothing and a completed commit
changes nothing beyond the moment the advisory lock releases.
While the exclusive lock is held — after acquisition, before the
gate transaction resolves — the child runs one more admitted
step, the severance sweep: a terminate-and-verify loop over
`pg_stat_activity` targeting every backend in the hub
database whose `application_name` carries the `gobby-hub-` marker
of a different lifecycle. The loop issues `pg_terminate_backend`
in its positive-timeout form — the form that waits for actual
process exit and returns false when the wait expires, because the
zero-timeout default only reports that the signal was sent, never
that the process died — then re-queries the marker predicate, and
the sweep completes only when that verification query returns
zero matching backends; a false return resolves through the
re-query, since a backend that vanished in a benign race no
longer matches and one that survived is re-signaled. Each sweep
statement runs under the transaction's `SET LOCAL`
bounds and is admitted against the remaining budget as a
five-second admission-cost entry; all gobby connections
authenticate as the one hub DSN role, and PostgreSQL lets a role
signal its own backends, so no extra privilege is involved. The
sweep is what retires a predecessor lifecycle's abandoned
workers — the finalizer-deadline fallback above can leave a
wedged worker awaiting a stalled statement on a checked-out
connection, which pool close explicitly leaves open and no
client-side act can settle — and its ordering closes the gap
exactly: a predecessor transaction already holding the shared
fence lock resolves before the exclusive grant, so the sweep
never fires mid-fenced-commit; every other predecessor backend
alive at sweep time dies before the gate transaction ends, and
the predecessor's closed pool can never mint another, so once the
exclusive lock releases there is no predecessor backend left to
acquire the fence and no path to a predecessor write after
ownership transfer. Termination signals and process exits are non-transactional — a
severed or rolled-back gate transaction that
already swept keeps its severances and the next gate's sweep is
an idempotent re-run — but a delivered signal proves nothing
about process death, which is why sweep completion is defined by
the empty verification re-query and never by the terminate call's
return value, and why the exclusive lock is still held at
completion: no matched backend outlives the sweep into the
post-release window. A sweep statement that expires at its bound,
residual matching backends at budget exhaustion, or a failed
sweep query is a gate failure, failing the boot closed within the
deadline. On an ordinary restart the sweep is a no-op — a
force-killed predecessor's sockets are already severed — and it
stays within the hub's single-owner model: it matches only the
current database and the hub marker, never another project's or
tool's connections. Exclusive acquisition succeeds only
once every predecessor terminal transaction has resolved, and
advisory locks release only at transaction end, so any snapshot
taken after the fence — including the recovery operation that
runs later inside subsystem initialization — observes every
predecessor terminal commit, including one PostgreSQL completed
after the client was SIGKILLed mid-COMMIT. Budget exhaustion
fails closed: a predecessor backend wedged while holding the
shared lock makes every attempt time out, and rather than
retrying forever or sweeping unsafely, the boot aborts with a
fatal diagnostic naming the holding backend from a `pg_locks`
join to `pg_stat_activity` — and that diagnostic is itself a live
query with no inherent deadline, so it runs on the child's
dedicated connection under the same `SET LOCAL` bounds and is
admitted against the same remaining budget as one more
five-second admission-cost entry; on
diagnostic timeout, failure, or budget exhaustion (the remaining
budget no longer covers its admission cost, so it is skipped),
the abort proceeds anyway with a fallback message carrying the
fence key and the diagnostic error or its budget-skip — the
whole gate, establishment and teardown included, resolves within
the one declared sixty-second deadline: by admission when the
statement bounds hold, by the enforced watchdog expiry — child
killed and reaped — when they do not. The abort path is complete
and settle-ordered, and round 26 made the gate site its owner:
the pre-server lifecycle has no cleanup of its own — the only
close-then-release sequence lives in the shutdown finalizer,
which exists only after Uvicorn setup
(`runner_lifecycle.py:170-258`,
`runner_lifecycle_shutdown.py:762-773`), the fatal
`except Exception` arm releases the pid claim without closing
the database, and caller cancellation bypasses that arm entirely
(`runner_lifecycle.py:283-288`) — so the gate await site in
`run_daemon` performs early-startup cleanup itself, for gate
failure and for cancellation alike, in one pinned order: the
helper's finally has already killed and reaped the child; the
gate site then closes `runner.database` — a close failure is
logged and never blocks the next step — then releases the owned
pid claim, then preserves the original outcome. Gate failure
logs the fatal gate diagnostic at the gate site and raises
`SystemExit(1)` directly, never routing through the
`except Exception` arm, so no double cleanup runs; cancellation
re-raises `CancelledError` to the embedded caller. On the fatal
shape the Uvicorn server is never constructed, no subsystem task
starts, no recovery operation runs, and the process exits
nonzero; an embedded host that catches the `SystemExit`, and an
embedded caller whose cancellation unwound the gate, each
continue with no gate process, no connection object, no open
database, no held pid claim, and no in-interpreter transaction
state left behind — the pid file is free, so a second lifecycle
in the same interpreter claims it and boots through its own gate
— and every durable subscriber row stays untouched for a later
boot once the wedged backend is resolved. The startup sweeps that follow a
successful fence then terminalize or
deliver whatever that snapshot shows, so a late predecessor
commit surfaces as an ordinary daemon-down terminal, delivered on
this boot rather than stranded until another restart.

Fix: route every bypass producer through the single 1.3 contract.
`_complete_self_terminated_run` and the enforcement fallback go through the
existing `AgentLifecycleMonitor.terminalize_successful_run` path where the
monitor is available, preserving each caller's `notify_result` payload,
completion message, and (for tmux) the capture-then-kill ordering; the
genuine no-monitor fallbacks — including both `agent_cancellation.py`
paths — await `deliver_and_cleanup_terminal_run` (1.3) instead of bare
`notify`, which also fixes the missing `run_id` on the killed-error payload
via the helper's central injection. `complete_and_notify_agent_run` itself
(`run_completion.py:16`) keeps its complete-run offload role but its
notify step becomes an awaited `deliver_and_cleanup_terminal_run` call —
which breaks its owner test's current contract:
`tests/agents/test_run_completion.py`
(`test_complete_and_notify_agent_run_offloads_complete_run`) asserts a
**direct** `completion_registry.notify(...)` await with a payload lacking
`run_id`, and is updated to the helper contract (1.4.8).
`SessionCoordinator.
_notify_agent_completion` schedules the helper coroutine instead of bare
`notify` (same `create_task` / `run_coroutine_threadsafe` bridging): the
hook thread still does not block, but notify and cleanup are now ordered
inside the one awaited chain, so cleanup can never precede delivery.
The scheduled chain is fire-and-forget by design — the hook thread
has already committed the terminal transition synchronously and
cannot await the loop-side chain — so its cancellation guarantee is
retention-based rather than scope-based, a code-proven next-boot
exclusion: the chain removes durable rows only on acknowledged
delivery, so cancellation of the scheduled task at loop shutdown, a
`run_coroutine_threadsafe` hand-off onto a closing loop, or the
no-loop skip branch (`session_coordinator.py:765-766`) each leave
every durable subscriber row retained, and the next boot's
acknowledged sweep — step two of the first executable startup
operation, ahead of every fallible optional initialization and
independent of monitor and pipeline-runtime availability (1.3) —
delivers them —
the same
delete-after-acknowledgement two-branch argument as the
shutdown-cancel exclusion above. No mid-run canceller targets the
scheduled task: `create_task` parents it to the loop, not to
whatever request task happened to be running, so only loop shutdown
cancels it (1.4.6).
`notify()` snapshots subscribers before awaiting callbacks, so cleanup
after notify cannot starve the wake fan-out; any residual `wait()` caller
that loses the race gets the existing typed `CompletionResultEvictedError`
(established semantics, task #15959). The startup sweep — now acknowledged
deliver-then-remove per 1.3 — stays as the backstop for runs that go
terminal while the daemon is down.

For the never-notify class: `cleanup_stale_runs` and
`cleanup_stale_pending_runs` return the transitioned run ids instead of
bare counts (their storage owner tests in
`tests/storage/test_storage_agents.py` and the CLI tests pinning
count-shaped returns in `tests/cli/test_cli_agents.py` /
`tests/cli/test_agents_coverage.py` update to the new contract). The
sweep-and-deliver chain lives in one shared acknowledged-sweep
operation, `run_acknowledged_stale_sweeps`, defined on the cleanup
handler beside the 1.3 helper: it runs the requested sweep
offload(s) and awaits `deliver_and_cleanup_terminal_run` for each
returned id with the terminal payload synthesized from the re-read
run record (`status`, `run_id`, error), matching the 1.3
startup-sweep synthesis, all inside one shielded delivery scope
(above). Every in-daemon caller — the monitor's `_check_loop` sweep,
the dispatcher heartbeat reap, the cleanup-handler wrapper and its
startup call, and the `POST /api/agents/cleanup` route (1.4.11) —
invokes that shared operation rather than the storage sweeps
directly, so a shutdown cancellation — the route's HTTP request task
is cancelled at daemon shutdown
(`runner_lifecycle_shutdown.py:211-226`) — waits out both the sweep
and every per-id delivery. The transition
commits inside the worker-thread call and the helper is awaited afterward
on the owning loop — exactly the §1.2 ordering property — and a failed
delivery retains the row for the startup sweep. The kill-path callers
(observe-continue, `cleanup_unattached_spawned_run`,
`_cancel_active_agents` on both its branches), `unregister_agent`, the
MCP kill surfaces (every non-self MCP kill whose kill step may have
committed the transition gets post-kill routing, independent of `stop`
and placed ahead of **every** exit — failure returns and kill-raised
exceptions included, since the kill closes the terminal before it can
fail and the shared `success=False` early return
(`agent_cancellation.py:177-188`,
`agents_lifecycle_tools.py:249-264`) otherwise strands a
capture-committed transition, and the kill await itself can raise
**after** that commit: past a successful `_close_tmux_session`,
`kill_agent` still probes and signals the process — the process-group
signal path catches only `ProcessLookupError` (`kill.py:531-543`), so
a `PermissionError` on a recycled or foreign PID escapes, and the
TERM wait/escalation path (`kill.py:545-565`) can likewise raise
through `_wait_for_pid_exit`'s signal-0 probe, the un-wrapped
identity re-check, or the SIGKILL escalation whose try also catches
only `ProcessLookupError`. Each surface therefore wraps its
capture-capable kill await inside the shielded delivery scope under
`BaseException` semantics, so that on any exception —
`CancelledError` included — it performs the terminal re-read plus
awaited helper before the exception or cancellation re-propagates,
with a failed re-read or delivery leaving the durable rows retained
for the startup sweep: `stop_agent_run` performs the terminal
re-read plus awaited helper before its kill-failure return, on its
kill-exception path before the exception propagates, and
whenever its terminalize step reports no transition, delivering the
terminal row with the 1.3 sweep-synthesized payload, so a
capture-preempted stop delivers on the failure-return, exception,
and no-transition shapes alike; `kill_agent(stop=True)` performs the
same re-read plus helper before its kill-failure return and on its
kill-exception path, and its
`terminalize_killed_agent_run` routes **both** of its no-transition
branches — the cancelled branch where `terminalize_cancelled_agent_run`
reports no transition and the error branch where `run_storage.fail`
returns `None` on an already-terminal row — through the same terminal
re-read plus awaited helper; `kill_agent(stop=False)` keeps its
no-explicit-terminalize semantics but performs the same re-read on
all three of its exits — the kill-failure return, the kill-exception
path, and the ordinary
workflow-stopped return — awaiting the helper on any capture-committed
transition; `debug=True` sets `close_terminal=False`, so no capture
terminalizer preempts and the explicit terminalize path delivers
canonically; self-termination stays
on `_complete_self_terminated_run`, 1.4.1), the
HTTP cancel route (the route's `finally` that today holds the
reconcile alone (`servers/routes/agents.py:734-737`) becomes a nested
chain — `try:` reconcile, `finally:` re-read the run and await the
helper on a terminal row — because route code can still raise around
a committed transition even with step-zero atomic storage
transitions: `kill_agent` can throw after the capture-policy
terminalizer committed, and the reconcile wrapper's own retry logic
or `manager.get` probe can throw after `LocalAgentRunManager.cancel`
returned or around an already-terminal row
(`servers/routes/agents.py:40-55`); the nesting awaits the helper
before any exception — `kill_agent`'s or reconciliation's —
propagates, covering the route-committed transition, the
kill-exception path, the reconcile-exception path, and an
already-terminal reconcile, and a failure in the re-read or delivery
step itself leaves the durable rows retained for the startup sweep), the
test-mode admin endpoints (`register_test_agent` after any synchronous
terminal write, `unregister_test_agent` after its `fail` — routed
through the helper rather than argued as exclusions, since the
registration window is real), and
the spawn/resume failure paths each await the helper after their
kill/transition step. For resume, both call branches re-read the run
after `_kill_spawned_tmux_session` returns: a terminal row (the
capture-policy transition can commit even when the policy reports
failure) awaits `deliver_and_cleanup_terminal_run` with the payload
synthesized from the run record — the 1.3 sweep synthesis — and a
still-active row (the policy failed before its transition) falls back
to `_fail_run`, whose own helper routing then delivers; no resume
failure leaves a run non-terminal or a terminal run undelivered.
The completion registry these helper calls need is threaded from its
owners rather than assumed in scope: `AgentRunner` exposes no registry
(`agents/runner.py:40-65`), so `spawn_agent_impl` takes the registry as
an explicit parameter and hands it to the deferred health check and the
failure-cleanup `_fail_run`
(`spawn_agent/_implementation.py:157-192,886-904`, `_health.py:81-115`,
`_failure_cleanup.py:91-109`) — and **every** direct caller supplies
it, not just the spawn factory (which already holds
`completion_registry`, `spawn_agent/_factory.py:463-510`): dispatch
spawn passes `services.completion_registry`
(`dispatch/spawn.py:203-205`), the cron executor passes
`self.services.completion_registry` — `init_servers` assigns the live
`ServiceContainer` to the executor (`runner_init/servers.py:81`) —
(`scheduler/executor.py:326-328`), and the HTTP spawn route passes
`server.services.completion_registry`
(`servers/routes/agent_spawn.py:339-341`), so a deferred-health
failure after any of those surfaces returned a successful `run_id`
still delivers to a registered waiter. Dispatch resume passes
`services.completion_registry` — the same handle
`dispatch/spawn_completion.py:61` already reads — through
`try_resume_daemon_stop_run` (`dispatch/daemon_resume.py:40-94`) into
`resume_agent_run`, which supplies the kill-helper re-read routing and
resume's `_fail_run`. Dispatch's kill-path cleanup is threaded the
same way: `execute_spawn_action` already holds the live `services`
handle (`dispatch/spawn_actions.py:31-44`) but the cleanup chain drops
it — `_cleanup_or_quarantine_spawned_run` invokes the callback with
only `run_id`/`db`/`error` (`spawn_actions.py:136-143`), the
dispatcher wrapper forwards exactly those (`dispatcher.py:451-457`),
and `cleanup_unattached_spawned_run` accepts nothing more
(`spawn_actions.py:178-183`) — so `execute_spawn_action` passes
`services.completion_registry` through
`_cleanup_or_quarantine_spawned_run` and the dispatcher wrapper into
`cleanup_unattached_spawned_run`, which gains a keyword-only
`completion_registry` parameter and performs the terminal re-read plus
awaited helper before **every** exit — the kill-exception and
kill-failure `False` returns (`spawn_actions.py:192-205`), the
fail-exception return, and the `fail`-returns-`None` already-terminal
path — because its `close_terminal=True` kill can capture-commit the
transition ahead of each of them; a `None` registry (e.g. bare test
wiring) performs no delivery there and leaves the durable rows
retained for the startup sweep. The websocket observe-continue producer gets an
explicit route to the same registry: `init_servers` passes
`runner.completion_registry` into `WebSocketServer.__init__`
(`runner_init/servers.py:129-147`), which stores it on the
`SessionControlMixin` state (`websocket/server.py:71-166`,
`session_control.py:32-58`), and `_release_source_session` reads it
from the mixin for its post-kill terminal re-read plus awaited helper
(`handlers/session_observe_continue.py:38-77`) — on the normal return
and on the kill-exception path alike: the handler converts a raising
`kill_agent` into `RuntimeError`
(`session_observe_continue.py:47-57`), and because the kill can
capture-commit the transition and then raise, the re-read plus
awaited helper runs before that converted exception propagates,
while a `CancelledError` — which that `except Exception` boundary
does not catch — takes the same shielded re-read-plus-helper path
and re-propagates unconverted after the scope settles; a
registry-less
construction (e.g. bare test wiring) performs no delivery on that
branch and leaves the durable rows retained for the startup sweep.
Helper idempotence (ISM dedup, no-delivery-map rule) makes all of this
safe even where a canonical path may also run. The CLI's
destructive direct-DB cleanup mode is removed outright: a reachability
probe cannot be made exclusive with daemon lifecycle — the daemon can
start, finish its startup terminal-row sweep, and stay live between
the CLI's probe and its writes, so the CLI would terminalize a
subscribed run outside the registry after the only sweep that revisits
terminal rows, stranding the waiter until another restart. Instead
`gobby agents cleanup` (non-dry-run) calls a daemon endpoint
(`POST /api/agents/cleanup`, `servers/routes/agents.py`) that invokes
the shared `run_acknowledged_stale_sweeps` operation (the 1.4.9
wiring, honoring the CLI's timeout argument) — so the route's request
task, which daemon shutdown cancels
(`runner_lifecycle_shutdown.py:211-226`), holds its sweeps and
per-id deliveries in the same shielded scope as every other
caller — and exits with an error directing to the
daemon's startup/periodic sweeps when the daemon is unreachable —
daemon-down stale rows are left untouched precisely so the next boot's
sweeps terminalize and deliver them; `--dry-run` stays read-only and
unrestricted. Correct **both** stale findings in
`docs/reviews/agents.md` as part of this deliverable: the `:76-80` blocker
(claims no runtime cleanup exists) and the `:207-211` finding (claims
`register()` replaces registrations and drops waiters — it merges,
`completion_registry.py:47-77`).

**Acceptance:**

- 1.4.1 - `end_agent_run` self-termination (tmux and non-tmux branches)
  leaves zero durable `completion_subscribers` rows and no registry entry for
  the run, with notification payload and capture-then-kill ordering
  preserved. symbol: `_complete_self_terminated_run`. file:
  `src/gobby/mcp_proxy/tools/agents_termination.py`. test:
  `tests/mcp_proxy/tools/test_agents.py`.
- 1.4.2 - Workflow-enforcement completion leaves zero durable rows and no
  registry entry on both the lifecycle-monitor branch and the no-monitor
  fallback. file: `src/gobby/workflows/engine/enforcement_completion.py`.
  test: `tests/workflows/test_agent_workflow_completion.py`.
- 1.4.3 - Every terminal mode — success, error, timeout, cancellation,
  explicit `end_agent_run`, workflow-enforcement completion, session-end
  coordinator completion, no-monitor cancellation, periodic stale-run
  timeout, stale-pending reap, kill-path close, capture-preempted MCP
  stop/kill, `unregister_agent`,
  HTTP cancel-route cancellation, test-mode admin terminalization, and
  spawn/resume failure — ends with zero durable rows and no registry
  entry when delivery succeeds; the startup sweep still covers
  daemon-down terminals. test: `tests/agents/test_agent_cleanup.py`.
- 1.4.4 - Restart recovery reflects the 1.3 restructuring: active-run
  subscriber rehydration and the acknowledged sweep run as the
  unconditional first-operation boot step, and the monitor-gated
  reconciliation's residual registration stays idempotent against it —
  no dropped waiters, no duplicate delivery. test:
  `tests/test_runner_lifecycle.py`.
- 1.4.5 - `docs/reviews/agents.md` reflects the actual state: bypass paths
  fixed, acknowledged deliver-then-remove sweep as backstop, and the
  `:207-211` register-replaces claim corrected to merge semantics. file:
  `docs/reviews/agents.md`.
- 1.4.6 - Deterministic `SessionCoordinator` ordering test: `complete_agent_
  run` schedules the helper chain; cleanup runs only after the awaited
  `notify` resolves inside that chain, the delivered map drives row
  removal, and the already-terminal early path (`:489`) re-notifies
  harmlessly (duplicate → no map → no row removal). Deterministic
  cancellation cases cover both scheduling branches: on the
  current-loop branch (`session_coordinator.py:756-758`) the
  scheduled helper task is cancelled after the synchronous terminal
  commit and before delivery — every durable subscriber row is
  retained and no row removal or registry cleanup has run; on the
  cross-thread branch (`:763-764`) the threadsafe-scheduled
  coroutine is cancelled before delivery, and the closed/absent-loop
  skip branch (`:765-766`) is exercised — rows likewise retained for
  the next-boot acknowledged sweep (first-operation per 1.3); and a
  settled-delivery companion
  pins that when the scheduled task or future completes,
  acknowledged delivery or retention has settled — rows removed only
  per the delivered map. symbol:
  `SessionCoordinator._notify_agent_completion`. file:
  `src/gobby/hooks/session_coordinator.py`. test:
  `tests/hooks/test_session_coordinator.py`.
- 1.4.7 - No-monitor cancel and killed-error paths deliver payloads
  carrying `run_id`, leave zero durable rows and no registry entry on
  successful delivery, and retain undelivered rows. With a waiter
  pre-registered via `wait_for_agent`: a capture-preempted
  `stop_agent_run` (the kill commits the transition, terminalize
  reports none) re-reads and delivers the terminal row through the
  helper; a capture-preempted `kill_agent(stop=True, debug=False)`
  delivers through `terminalize_killed_agent_run`'s no-transition
  routing for **both** request shapes — cancelled (terminalize reports
  no transition) and error (`run_storage.fail` returns `None`); a
  kill-result failure after a successful terminal close (`success=False`
  without `KILL_ERROR_NO_TARGET_PID`, e.g. "Terminal closed but no
  target PID was found to verify process death", `kill.py:473-487`)
  delivers the capture-committed terminal row before the failure result
  returns — for `stop_agent_run` and for both `stop` values of
  `kill_agent`; a deterministic kill stub that commits a terminal
  transition and then raises has the terminal row re-read and
  delivered through the helper before the exception propagates — for
  `stop_agent_run` and for both `stop` values of `kill_agent` — with
  the original exception still surfacing to the caller afterward; a
  deterministic commits-then-cancel case gates the storage worker
  behind an event, cancels the surface's task after the worker has
  started, allows the terminal commit, and asserts the re-read plus
  acknowledged delivery (or failed-delivery retention) settles
  before `CancelledError` surfaces to the caller — again for
  `stop_agent_run` and for both `stop` values of `kill_agent`; and
  `kill_agent(stop=False)` delivers any
  capture-committed transition before returning while never explicitly
  terminalizing. All cases pin
  acknowledged row cleanup and failed-delivery retention, and
  companion cases pin `debug=True` (no capture terminalizer preempts;
  the explicit terminalize path delivers canonically) and
  self-termination (`_complete_self_terminated_run`, 1.4.1) semantics
  unchanged. file: `src/gobby/mcp_proxy/tools/agent_cancellation.py`,
  `src/gobby/mcp_proxy/tools/agents_lifecycle_tools.py`. test:
  `tests/mcp_proxy/tools/test_agent_cancellation.py`,
  `tests/mcp_proxy/tools/test_agents.py`.
- 1.4.8 - `complete_and_notify_agent_run`'s owner test asserts the helper
  contract instead of a direct `notify` await: the payload carries
  `run_id`, successful delivery removes the run's durable rows and cleans
  the registry entry, and an undelivered subscriber's row is retained.
  A commits-then-cancel case gates the complete-run worker inside
  the offload, cancels the awaiting task after the worker has
  started, and asserts the completion commit, helper delivery, and
  row cleanup settle before `CancelledError` surfaces.
  symbol: `complete_and_notify_agent_run`. file:
  `src/gobby/agents/run_completion.py`. test:
  `tests/agents/test_run_completion.py`.
- 1.4.9 - `cleanup_stale_runs` and `cleanup_stale_pending_runs` return
  transitioned run ids, and every in-daemon caller (monitor `_check_loop`
  sweep, dispatcher heartbeat reap, cleanup-handler wrapper and its
  startup call, and the `POST /api/agents/cleanup` route, 1.4.11)
  awaits `deliver_and_cleanup_terminal_run` per id through the shared
  `run_acknowledged_stale_sweeps` operation.
  Deterministic waiter-liveness test: a subscriber registered via
  `wait_for_agent` before the sweep runs receives the timeout wake when
  the sweep terminalizes its run, durable rows are removed only on
  acknowledged delivery, and a failed delivery retains the row for the
  startup sweep. A per-id transition failure inside `cleanup_stale_runs`
  is caught and logged, transitions nothing for that id (atomic
  rollback, 1.4.15), does not abort the sweep, and leaves earlier
  transitioned ids in the returned list for delivery, with the failed
  id re-matched on the next cycle. A commits-then-cancel case per
  caller chain cancels the sweep-owning task after the sweep worker
  has started: the transitions commit, every transitioned id still
  gets its awaited helper delivery — rows removed only on
  acknowledgment, retained on failed delivery — and
  `CancelledError` surfaces only after the scope settles; for the
  route chain the case cancels the request task serving
  `POST /api/agents/cleanup` after its sweep worker has started —
  matching the shutdown request-task cancel
  (`runner_lifecycle_shutdown.py:211-226`) — and asserts every
  committed id reaches acknowledged delivery or retained-row retry
  state before `CancelledError` surfaces.
  symbol: `cleanup_stale_runs`. file:
  `src/gobby/storage/agents/_cleanup.py`,
  `src/gobby/agents/agent_cleanup.py`. test:
  `tests/storage/test_storage_agents.py`,
  `tests/agents/test_lifecycle_monitor.py`,
  `tests/agents/test_agent_cleanup.py`,
  `tests/servers/routes/test_agents_routes.py`,
  `tests/dispatch/test_dispatcher.py`.
- 1.4.10 - The kill-path callers (websocket observe-continue,
  `cleanup_unattached_spawned_run`, `_cancel_active_agents` monitor and
  no-monitor branches), `unregister_agent`, and the spawn/resume failure
  paths each end in the helper: delivered payloads carry `run_id`, zero
  durable rows and no registry entry remain on successful delivery, and a
  duplicate delivery against an already-notified run is a no-op (no rows
  removed, registry cleanup idempotent). For resume's
  `_kill_spawned_tmux_session` branches specifically: with a spawned
  tmux session, the runtime-persist-failure and start-skipped branches
  each deliver the capture-committed terminal row through the helper
  (acknowledged delivery removes the rows; a failed delivery retains
  them), and a capture-policy failure that leaves the run active falls
  back to `_fail_run` plus the helper. The completion registry reaches
  every such site through the 1.4 wiring (every direct
  `spawn_agent_impl` caller — spawn factory, dispatch spawn, cron
  executor, HTTP spawn route — supplies its owning registry into
  `spawn_agent_impl` → health check and failure cleanup; dispatch
  resume services → `try_resume_daemon_stop_run` → `resume_agent_run`;
  `init_servers` → `WebSocketServer` → `SessionControlMixin` for
  observe-continue; `execute_spawn_action` services →
  `_cleanup_or_quarantine_spawned_run` → dispatcher wrapper →
  `cleanup_unattached_spawned_run` for dispatch kill-path cleanup), and
  integration cases register a waiter through
  the real registry and exercise each spawn/resume failure path in
  both monitor-present and no-monitor wiring, asserting the wake
  fires. For the direct dispatcher, cron, and HTTP spawn surfaces
  specifically: a deferred-health failure after the surface returned a
  successful `run_id` wakes the pre-registered waiter, and a failed
  delivery retains the durable rows; a websocket observe-continue case
  pins acknowledged cleanup and failed-delivery retention through the
  mixin-held registry, with a companion case whose kill stub commits a
  terminal transition and then raises pinning delivery through the
  helper before the converted `RuntimeError` propagates, plus a
  cancellation companion — the observing task cancelled after a
  gated kill stub commits — pinning that delivery settles and
  `CancelledError` propagates unconverted; and a
  dispatch-cleanup case with a waiter
  pre-registered through the real registry pins that a kill failure
  (and a caught kill exception) after the capture terminalizer
  committed the transition still delivers the terminal row before the
  `False` return — acknowledged delivery removes the durable rows, a
  failed delivery retains them, and a `None` registry performs no
  delivery and retains them. test:
  `tests/servers/websocket/test_resume_blocked.py`,
  `tests/dispatch/test_dispatcher.py`,
  `tests/dispatch/test_daemon_resume.py`,
  `tests/scheduler/test_cron_executor.py`,
  `tests/servers/routes/test_agent_spawn_routes.py`,
  `tests/build/test_build_stop.py`,
  `tests/mcp_proxy/tools/test_agents.py`,
  `tests/mcp_proxy/tools/spawn_agent/test_health.py`,
  `tests/mcp_proxy/tools/spawn_agent/test_error_handling.py`,
  `tests/agents/test_resume_executor.py`.
- 1.4.11 - `gobby agents cleanup` performs no direct-DB terminal writes
  in any daemon state: non-dry-run calls the daemon cleanup endpoint
  (`POST /api/agents/cleanup`, which invokes the shared
  `run_acknowledged_stale_sweeps` operation — the same shielded
  scope as every other in-daemon caller, 1.4.9 — with the CLI's
  timeout argument) and exits with an error when
  the daemon is unreachable; `--dry-run` stays read-only. The
  concurrent-start race is closed by construction — no CLI code path
  terminalizes a run outside the daemon — and the daemon-down chain
  "stale run left untouched → next boot's sweeps terminalize and
  deliver" is covered end-to-end; the route's commits-then-cancel
  cancellation case is pinned in 1.4.9. file: `src/gobby/cli/agents.py`.
  test: `tests/cli/test_cli_agents.py`,
  `tests/cli/test_agents_coverage.py`,
  `tests/servers/routes/test_agents_routes.py`,
  `tests/test_runner_lifecycle.py`.
- 1.4.12 - The HTTP cancel route nests the chain — reconcile in `try`,
  terminal re-read plus awaited helper in `finally` (commit first,
  delivery second): a waiter registered via `wait_for_agent` before the
  cancel receives the cancellation wake, durable rows are removed only
  on acknowledged delivery, a failed delivery retains the row,
  reconciling an already-terminal run (e.g. a preceding capture-path
  transition) re-delivers idempotently (duplicate → no delivery map →
  no row removal), a raising `kill_agent` still commits the reconcile
  and awaits the helper before the exception propagates, and a
  reconcile wrapper that raises after `LocalAgentRunManager.cancel`
  returned (the atomic transition committed, 1.4.15) — or a raising
  `manager.get` probe on an already-terminal
  run — still has the helper awaited on the re-read terminal row before
  propagation, with a failed re-read or delivery leaving the durable
  rows retained for the startup sweep. symbol:
  `cancel_agent_run`. file: `src/gobby/servers/routes/agents.py`. test:
  `tests/servers/routes/test_agents_routes.py`.
- 1.4.13 - Both test-mode admin endpoints enter the helper after any
  terminal write: `unregister_test_agent` on an active subscribed run
  delivers the failure wake and cleans acknowledged rows, and
  `register_test_agent` with a terminal `status` delivers for a waiter
  registered in the create→terminalize window; non-terminal
  registrations create no delivery. file:
  `src/gobby/servers/routes/admin/_testing.py`. test:
  `tests/servers/routes/test_admin_extended.py`.
- 1.4.14 - Shutdown-to-restart branch proof pinned: starting from a
  strict `wait_for_agent` durable row, the capture-policy branch
  (`kill_agent(close_terminal=True)` transitions first,
  `transitioned_here` false, no shutdown notify) leaves the row intact
  through shutdown and the next boot's acknowledged sweep delivers and
  removes it; the `terminalize_cancelled_run`-transition branch delivers
  through the helper at shutdown — an acknowledged delivery removes the
  row, and a failed ISM persist retains it for the next boot's sweep.
  symbol:
  `_cancel_active_agent_runs_for_shutdown`. file:
  `src/gobby/runner_lifecycle_agents.py`. test:
  `tests/test_runner_lifecycle.py`.
- 1.4.15 - Terminal-transition atomicity: with an injected failure in
  `_expire_sessions_for_run_ids` or the in-transaction row re-read
  after each of the four terminal UPDATEs (`complete`, `fail`,
  `timeout`, `cancel`), the method raises and no terminal commit
  occurred — the run re-reads as its prior non-terminal status and a
  subsequent un-faulted call transitions and returns it — and the
  zero-rowcount already-terminal guard still returns `None` without
  modifying the row. symbol: `_AgentRunLifecycleMixin.complete`. file:
  `src/gobby/storage/agents/_lifecycle.py`. test:
  `tests/storage/test_storage_agents.py`.
- 1.4.16 - Ambient-nesting guard: each of the four terminal methods
  called inside an open `db.transaction()` block raises
  `TerminalTransitionNestedError` without executing the terminal
  UPDATE — after the outer transaction commits, the run still reads
  as its prior non-terminal status — and the same call outside any
  ambient transaction transitions and returns the run. symbol:
  `_AgentRunLifecycleMixin.cancel`. file:
  `src/gobby/storage/agents/_lifecycle.py`. test:
  `tests/storage/test_storage_agents.py`.
- 1.4.17 - Cancellation-shielded delivery scope, pinned at the
  scope-entry boundary: cancelling the caller before it invokes the
  scope performs no transition work; with the transition worker gated
  behind an event, cancelling the caller task after the scope owns
  the offload — both after the worker has started and while it is
  still queued and unstarted — lets the terminal commit, re-read, and
  acknowledged delivery settle before `CancelledError` re-propagates;
  repeated cancellation during settlement does not sever the chain;
  and a capture-offload case pins that a mid-capture cancellation
  settles the capture-policy terminal commit before `CancelledError`
  re-propagates to the kill surface, whose shielded re-read plus
  helper delivers the committed row. symbol:
  `shielded_terminal_delivery`. file:
  `src/gobby/agents/agent_cleanup.py`, `src/gobby/agents/capture.py`.
  test: `tests/agents/test_agent_cleanup.py`,
  `tests/agents/test_capture.py`.
- 1.4.18 - Shutdown quiescence and closed-set drain: with the database
  executor saturated so a scope-owned terminal transition sits queued
  and unstarted, the producer's request task cancelled, and shutdown
  cleanup initiated, `drain_shielded_terminal_deliveries` settles the
  owned transition — terminal commit, re-read, and acknowledged
  delivery or retention — before any executor work is revoked; a
  second scope entering after the drain's first snapshot (executor
  still saturated) is settled by the stable-empty loop the same way; a
  scope invocation after the admission close performs no transition
  work — its run re-reads pre-terminal and every durable subscriber
  row is retained; and a pending deferred health-check task is
  cancelled and awaited in the graceful phase, with one cancelled
  mid-scope settling its owned transition before the drain begins.
  symbol: `drain_shielded_terminal_deliveries`. file:
  `src/gobby/agents/agent_cleanup.py`,
  `src/gobby/runner_lifecycle_shutdown.py`,
  `src/gobby/mcp_proxy/tools/spawn_agent/_health.py`. test:
  `tests/test_runner_lifecycle.py`,
  `tests/mcp_proxy/tools/spawn_agent/test_health.py`.
- 1.4.19 - Finalizer settlement sequence: on the normal path, on
  deadline expiry before async cleanup starts, and on the deadline
  landing mid-drain, `shutdown_daemon_services`' finalizer closes
  admission idempotently, runs the shielded-delivery re-drain, then
  the executor barrier — with admission close and the synchronous
  loop-side close-and-revoke's queued-work revocation ahead of
  `runner.database.close()` in all three shapes (the revocation
  atomic with the executor's guard flag under the executor lock,
  so no observer of a set guard can precede completed
  revocation), the completed dedicated-thread worker join
  additionally ahead of close on the within-budget branch, and
  the expiry branch proceeding to close without awaiting the
  join; on deadline expiry before async cleanup
  starts, a producer invoking the delivery scope after finalizer
  settlement begins performs no transition work — no executor
  submission and no in-flight entry, so nothing races the barrier
  or the database close, and its run resolves through next-boot
  retention; with the deadline landing mid-drain and every
  statement bound holding — the within-budget branch — a queued
  unstarted complete-run offload, a queued unstarted capture
  offload, a started complete-run offload, and a started capture
  offload each settle fully — commit, terminal re-read, and
  acknowledged delivery complete while the database is still open,
  each asyncio-owner finalizer runs, and the in-flight set is
  empty by settlement before the database closes — with no
  terminal commit landing after close in that branch; cancelling
  the `shutdown_daemon_services`
  task while the finalizer awaits the drain and again while it
  awaits the barrier — including a second cancellation during
  settlement — leaves the owned settlement task running to
  completion, with the database close and pid cleanup settled
  before the cancellation re-propagates to the caller; a
  delivery-phase statement whose bound expires — the wake session
  lookup, the dedup read, or the message insert — settles its
  scope in the retained-row state and the drain still reaches
  stable empty, and a live-nudge callback that never returns is
  cut off at the client-side deadline with the durable
  notification already stored and the scope acknowledged; the worker join
  executes on the dedicated barrier thread and the loop stays
  responsive throughout (a probe task scheduled during the
  barrier runs before the database closes); a deterministic
  interleaving test pauses the first `shutdown` caller between
  its guard-flag write and the underlying pool shutdown — an
  injected hook inside the executor lock — while the expiry
  branch invokes close-and-revoke concurrently, and proves the
  expiry caller returns only with queued futures already
  cancelled, before the database closes and with the join never
  awaited; the re-drain and barrier run under
  the declared ten-second client-side finalizer deadline measured
  at finalizer entry — with a scope-owned terminal-transition
  transaction that never returns from its first `SET LOCAL`, from
  COMMIT, or from ROLLBACK, and separately a delivery transaction
  driven through the real registry-notify → wake chain that never
  returns from the same three shapes — pinned for both the
  durable-wake dedup-and-insert transaction and the delivered-row
  removal transaction — the stall wedges only its executor
  worker: a probe task scheduled during the stall runs, the
  deadline expires, each
  abandoned scope is logged by run id and detached — its entry
  discarded from the tracked in-flight set, the discard idempotent
  against the abandoned task's own eventual finalizer, the set
  left empty for the successor's 1.4.20 assertion — with the
  scope's outcome proven to land on its pinned branch
  (retained-row when the stall precedes acknowledgement,
  acknowledged with rows present or absent when it lands in the
  removal COMMIT) before detachment is relied on, and database
  close and pid cleanup still run in the pinned order; abandoned
  scopes' durable rows are pinned to
  delete-only-after-acknowledged-delivery, not universal
  retention — a scope abandoned before acknowledged delivery
  keeps its subscriber rows, a scope abandoned during its
  post-acknowledgement row-removal COMMIT passes with the rows
  present or absent under both acknowledgement reasons, pinned
  separately: for `ism_persisted` the stored notification makes a
  surviving row an idempotent redelivery and an absent row a
  completed removal, and for `session_not_found` — where no
  notification was stored — a surviving row is reclassified
  `session_not_found` by the next boot's sweep and removed while
  an absent row lost nothing because no session can consume it;
  and no path removes a row without a prior
  acknowledged delivery; cancelling `shutdown_daemon_services` while the
  finalizer waits out a stalled settlement transaction settles
  database close and pid cleanup before the cancellation
  re-propagates; after a successor gate's severance sweep
  terminates the abandoned backend, the wedged executor worker
  unwinds with a connection error and the executor join
  completes; a worker join that outlives the budget leaves the
  finalizer proceeding through database close and pid release
  without awaiting it — close-and-revoke already executed ahead
  of the join — with the barrier thread
  leaked as a daemon thread and the loop closing without
  stranding on asyncio's default executor; subprocess coverage
  pins the standalone shape — a real daemon process whose
  settlement transaction is wedged in a client-unbounded shape
  receives its shutdown signal and exits within the backstop
  bound through the expiry-branch `os._exit`, with the pid file
  released and the outcome logged, and a successor process then
  acquires the pid claim and completes its gate while the old
  process's workers are still wedged — and the embedded shape:
  cancellation and SystemExit against the same wedge leave
  `run_daemon` returning to the host with the pinned order
  complete and the loop closed, workers leaked until severance;
  and the finalizer ordering ends
  with `cleanup_pid_file` releasing the singleton claim only after
  drain, close-and-revoke with its branch-scoped join, and
  database close. symbol:
  `shutdown_daemon_services`. file:
  `src/gobby/runner_lifecycle_shutdown.py`,
  `src/gobby/storage/executor.py`,
  `src/gobby/runner.py`. test:
  `tests/test_runner_lifecycle.py`,
  `tests/storage/test_database_executor.py`,
  `tests/events/test_wake.py`.
- 1.4.20 - Managed-executor choke point: with production wiring, the
  complete-run offload inside `complete_and_notify_agent_run`,
  every capture storage offload, and every delivery-path
  transaction under the finalizer proof — the wake session
  lookup, the dedup-and-insert transaction, the residual SDK
  lookups, and the subscriber-row removal — execute on the
  managed `DatabaseExecutor` — the seam-injected callable
  observes each call — with the bare-bridge default preserved
  when the seam is unset; the coordinator's hook-thread terminal commit goes through
  the module-level synchronous seam function — inline invocation by
  default, the executor's sync `submit` under production supply —
  and blocks on the returned future, with no executor parameter on
  `SessionCoordinator.__init__` and no edits to the hook factory,
  hook manager, or app-lifecycle wiring; a queued coordinator
  submission revoked by the barrier raises
  `concurrent.futures.CancelledError` from `Future.result()` and a
  post-shutdown submission raises `RuntimeError`, each caught by
  name on the hook thread; and in both shapes, as for a queued
  complete-run offload and a queued capture offload against a
  shut-down executor, nothing commits — the run stays active and
  every durable subscriber row is retained for the next boot's
  stale sweeps; and ownership precedes construction and is
  explicit: the pid claim is resolved before any `GobbyRunner`
  construction in every supported entry shape — `main()`
  already claims first (`runner.py:297-315`), `run_gobby`
  gains the embedded claim ahead of `GobbyRunner(...)`, with a
  contended claim logging and returning before construction
  and an `OSError` keeping the documented fail-open-unlocked
  semantic, and `run_daemon`'s embedded claim block is
  deleted — and the requirement is enforced by signature:
  resolved ownership is an explicit value, a held
  `PidFileClaim` or a distinct fail-open-unlocked resolution
  produced only by the `OSError` branch, carried as a
  required, non-default parameter of both `GobbyRunner.run`
  and `run_daemon`, replacing the
  `pid_claim: PidFileClaim | None = None` defaults
  (`runner.py:197-200`; `runner_lifecycle.py:111`), so no
  caller reaches the `_startup_tracker` write, the
  signal-handler installation, or the terminal
  app-context-clearing `finally` without passing a
  resolution; every direct caller — the bare `runner.run()`
  and `run_daemon(runner)` sites in
  `tests/test_runner_lifecycle.py`,
  `tests/test_runner_pid_file.py`, and
  `tests/test_runner_shutdown.py` — is swept to resolve
  ownership before constructing the runner (a per-test claim
  on the isolated home's pid file, or the explicit fail-open
  resolution where the test targets unlocked behavior), and a
  signature-witness test pins that the ownership parameters
  of `GobbyRunner.run` and `run_daemon` have no default;
  construction failure is transactional while the resolved
  ownership is still held, through a construction rollback
  ledger owned by `GobbyRunner.__init__`: each init stage
  appends an undo entry immediately after each
  rollback-relevant install — the hub database by close and
  the `DatabaseExecutor` by shutdown-and-join
  (`runner_init/storage.py:106-112`), the lifecycle-owned
  telemetry attachments (below), the memory stack — the
  `VectorStore` and the `MemoryManager` with its Falkor
  client (`runner_init/services.py:117-173`,
  `memory/manager.py:66-193`) — by their asynchronous
  `close()` (`memory/manager.py:309-316`), the tmux module
  helpers by a new reset in `agents/tmux/__init__.py`
  (installed at `runner_init/orchestration.py:136`, globals
  at `agents/tmux/__init__.py:59-67`), the published app
  context (`runner_init/servers.py:79-112`), the
  tool-summarizer module globals by a new reset in
  `utils/tool_summarizer.py` (installed at
  `servers/http.py:237-239`, globals at
  `utils/tool_summarizer.py:25-35`), and the agent-event
  and pty/tmux output callbacks
  (`runner_broadcasting.py:113-231`) — and a failing stage
  unwinds the ledger last-in-first-out, driving
  asynchronous entries with `asyncio.run` under the same
  five-second per-close bounds normal shutdown uses
  (`runner_lifecycle_shutdown.py:523-539`; no event loop
  runs during construction), re-raising only after the
  unwind completes; telemetry rolls back by ownership, not
  by `shutdown_providers`: the OpenTelemetry API tracer and
  meter providers and the LLM instrumentors are
  interpreter-latched — installed once by `init_telemetry`
  (`runner_init/storage.py:148`,
  `telemetry/__init__.py:95-101`) through one-shot API
  setters, never torn down by rollback, and provider
  acquisition in `telemetry/providers.py` reuses an
  already-installed API-global provider instead of
  constructing a shadow the setter would reject — while the
  lifecycle-owned `SpanStorage` span processor is shut down
  and its interpreter latch reset
  (`telemetry/providers.py:54-69`) and health metrics are
  disabled via `configure_health_metrics(enabled=False)`
  (`telemetry/__init__.py:107`); `run_gobby` releases any
  claim it acquired, idempotently, only after that
  rollback-complete propagation, when construction or
  startup raises — the deterministic missing-config
  `FileNotFoundError` (`runner_init/storage.py:60-65`)
  never reaches `run_daemon`'s release helper
  (`runner_lifecycle.py:138-143`), and `main()`'s terminal
  release (`runner.py:321-325`) covers only claims `main()`
  owns, after the same rollback — with regression tests in
  which the claim succeeds and an injected stage failure
  raises after database-and-executor creation, after the
  memory-stack initialization, after `configure_tmux`, and,
  separately, after `set_app_context` at `HTTPServer`
  construction (past the summarizer-global publish), each
  proving a contender claim contends until rollback
  completes, no executor thread, open hub pool, published
  app context, live Falkor or vector-store client, tmux or
  summarizer module global, or lifecycle span processor
  survives, health metrics are disabled, the OpenTelemetry
  API global still returns the live interpreter provider,
  and a subsequent same-interpreter lifecycle acquires the
  claim, starts cleanly, and attaches its own exporter so
  an emitted span reaches the successor's `SpanStorage`,
  with the pre-resource missing-config shape retained
  — while the production seam supply and admission
  lifecycle stay pinned at the activation step `run_daemon`
  invokes only after the boot gate (1.4.21) has completed,
  still before the Uvicorn server object exists and before any
  producer runs — the step
  points the async terminal-offload seam at the managed
  executor's `run` and the synchronous seam at its `submit`, so
  no serving daemon leaves either seam at its bare default,
  asserts the prior lifecycle's in-flight set is empty, and
  reopens the admission boundary — an assertion that holds
  unconditionally because both predecessor exits leave the set
  empty (settlement empties it within the finalizer budget, and
  the expiry branch's detach (1.4.19) empties it at abandonment)
  and because a contender never constructs — and, of the
  delivery-path state this plan introduces, `GobbyRunner`
  construction supplies only instance-local
  wiring (the `run_db` defaults and the dispatcher's executor
  handle) and writes no module seam and no admission state; two
  same-interpreter contention tests pin the pre-construction
  boundary: with daemon A serving and holding an in-flight
  scope, and again with A's in-flight set empty, a contended
  embedded entry (`run_gobby`) in that same interpreter returns
  cleanly with `GobbyRunner` construction observed never to
  run, no assertion fired, and daemon A's full global surface
  intact — its admission state, both module seams, the global
  app-context identity, its signal routing, and the tmux module
  helpers unchanged, with no second hub pool or executor
  thread left open — and A then completing a terminal
  transition with acknowledged delivery; a third case pins the
  fail-open shape: a claim raising `OSError` proceeds unlocked
  before construction, preserving today's sole-runner
  semantic; with daemon A instead
  settled and shut down in
  an interpreter, a daemon B whose claim resolves ahead of its
  construction, run through its gate and activation in that
  same interpreter,
  enters the delivery scope and completes a terminal transition
  with acknowledged delivery; and starting instead from a
  finalizer-deadline-abandoned scope in daemon A, daemon B
  acquires the pid claim, then initializes,
  runs its boot gate whose severance sweep terminates
  daemon A's wedged backend, then activates with the empty-set
  assertion passing, daemon A's worker unwinds with a
  connection error touching no daemon-B seam, and daemon B then
  completes its own terminal transition with acknowledged
  delivery — one same-interpreter test pinning the full recovery
  loop. symbol:
  `complete_and_notify_agent_run`. file:
  `src/gobby/agents/run_completion.py`,
  `src/gobby/agents/capture.py`, `src/gobby/agents/agent_cleanup.py`,
  `src/gobby/events/wake.py`,
  `src/gobby/storage/executor.py`,
  `src/gobby/hooks/session_coordinator.py`,
  `src/gobby/runner_init/orchestration.py`,
  `src/gobby/runner_init/servers.py`,
  `src/gobby/runner_init/services.py`,
  `src/gobby/runner_init/storage.py`,
  `src/gobby/agents/tmux/__init__.py`,
  `src/gobby/utils/tool_summarizer.py`,
  `src/gobby/telemetry/providers.py`,
  `src/gobby/runner_lifecycle.py`,
  `src/gobby/runner_broadcasting.py`,
  `src/gobby/runner.py`. test:
  `tests/agents/test_run_completion.py`,
  `tests/agents/test_capture.py`,
  `tests/events/test_wake.py`,
  `tests/storage/test_database_executor.py`,
  `tests/hooks/test_session_coordinator.py`,
  `tests/test_runner_init.py`,
  `tests/test_runner_lifecycle.py`,
  `tests/test_runner_pid_file.py`,
  `tests/test_runner_shutdown.py`.
- 1.4.21 - Predecessor fence and terminal-operation bounds: each of
  the five terminal-status transactions — the four lifecycle
  transitions and the stale-pending bulk transaction — issues
  `SET LOCAL statement_timeout`, `SET LOCAL lock_timeout`, and
  `pg_advisory_xact_lock_shared` on the fence key through the
  shared bounded-transaction helper as its first statements, and
  concurrent terminal transitions do not serialize against each
  other; the delivery-path database work — the wake session
  lookup, the dedup read and message insert in one helper-owned
  transaction, the SDK agent-runs lookup, the subscriber-row
  removal, and the bounded terminal re-read — runs under the
  helper's bounds through call-site wrappers, each executed
  through the managed executor offload off the event-loop thread
  (1.4.20), with the message
  manager and its ambient-transaction callers byte-for-byte
  unchanged; with a wake session read that never returns, a dedup
  read that never returns, a dedup fallback listing that never
  returns, and an SDK lookup that never returns, each expires at
  its bound — the durable phases surface the expiry through
  `_send_ism`'s handler into retained-row settlement instead of
  falling into an unbounded fallback, the nudge phase degrades
  best-effort with the notification already stored, and the
  per-session wake lock is released so a subsequent wake for the
  same session proceeds; the live-nudge phase in
  `WakeDispatcher.wake` expires at its client-side deadline into
  the established failure shape with the durable notification
  already stored; the hub pool's connection kwargs carry
  `connect_timeout` and TCP keepalive settings, and the
  post-`PoolTimeout` retry path performs no validation query —
  with a pooled connection whose validation query would never
  return, acquisition fails within the bounded two-attempt window;
  `run_daemon` awaits the exclusive fence gate after the pid claim
  and before the Uvicorn server object exists, so the fence
  resolves before any request handler, subsystem task, or
  shared-mode writer of this process can run; the gate body runs
  in a terminable child process holding one dedicated direct
  connection — never a hub-pool connection — so hub-pool
  starvation cannot slow the gate and pool close never waits on
  it, and the boot side kills and reaps that child before any
  abort or unwind path reaches database close or pid release;
  with a perpetual holder of the shared fence, each attempt
  expires at its five-second lock wait, admission control admits
  establishment, attempts, backoffs, and the diagnostic only
  while the remaining budget covers their bounded admission
  costs, and startup fails closed within the declared
  sixty-second gate deadline — the server never starts, no
  recovery operation runs, the database closes, the pid claim
  is released, and every durable subscriber row is preserved;
  with a holder diagnostic query that never returns, the
  diagnostic expires at its own bound inside the gate deadline
  and the abort proceeds with the fallback message naming the
  fence key and the diagnostic error — same server-never-started,
  pid-released, rows-preserved outcome; with child-side gate
  work that never returns — four deterministic shapes: a first
  `SET LOCAL` statement that never returns, a lock wait whose
  server-side `lock_timeout` never fires, a COMMIT that never
  returns, and a ROLLBACK that never returns — the watchdog
  deadline expires while the child is still in flight, the boot
  side kills and reaps the child and aborts within the declared
  deadline with the fallback message recording the deadline
  expiry, and the severed in-flight fenced transaction resolves
  server-side by whichever outcome applies — severance before
  COMMIT rolls it back, while a COMMIT the server had already
  accepted may complete after the client dies, committing no row
  change and releasing the advisory lock at commit, because no
  gate statement writes a row — both outcomes landing the same
  server-never-started, database-closed, pid-released,
  rows-preserved outcome; the gate's severance sweep runs while
  the exclusive lock is held and terminates — verified dead, not
  merely signaled — every hub-marker
  backend of another lifecycle in the current database — with an
  abandoned predecessor worker wedged before its fence
  acquisition, the sweep kills its backend and its transaction
  never commits, while a predecessor transaction already holding
  the shared fence blocks the exclusive acquisition until it
  resolves and its commit is observed by the post-fence snapshot;
  a matched backend whose exit is delayed past the first signal
  keeps the sweep incomplete — the exclusive lock stays held until
  the verification re-query shows no matching backend, is never
  released with a signaled-but-alive backend present, and such a
  backend never acquires the shared fence after the gate
  completes;
  the sweep spares the successor's own lifecycle marker and the
  gate connection's `gobby-gate-` marker, and a sweep statement
  that expires at its bound, residual matching backends at budget
  exhaustion, or a failed sweep query fails the gate closed within
  the declared deadline; with an embedded caller cancelling the
  gate await mid-stall, the helper's finally kills and reaps the
  child before the cancellation unwinds, the gate site then
  closes the database and releases the pid claim — child-reap,
  database-close, pid-release order — before `CancelledError`
  re-propagates, no gate process or
  connection object survives in the interpreter, the hub pool
  was never touched by the gate, and a second daemon lifecycle
  in the same interpreter claims the freed pid file and proceeds
  to a clean exclusive
  acquisition once the server resolves the severed transaction;
  with an embedded host catching the `SystemExit` of the fatal
  abort after watchdog expiry — raised directly at the gate site,
  never through the `except Exception` arm — the child was
  already killed and reaped, the gate site closed the database
  and released the pid claim in that order before raising, a
  database close that itself raises is logged without blocking
  the pid release, and no gate process, connection object, open
  database, or held pid claim survives into the host's continued
  interpreter, whose second lifecycle claims the pid file and
  starts safely through its own gate; and the
  indeterminate-COMMIT interleaving is pinned for both writer
  shapes — with the OS pid lock released while a predecessor
  backend's terminal COMMIT is still unresolved server-side, once
  from a lifecycle transition and once from the stale-pending bulk
  transaction, the replacement boot blocks on the fence until that
  transaction resolves, and its post-fence sweep observes the
  late-committed terminal row and delivers it, leaving no durable
  subscriber row stranded past this boot.
  symbol: `_AgentRunLifecycleMixin.complete`. file:
  `src/gobby/storage/agents/_lifecycle.py`,
  `src/gobby/storage/agents/_cleanup.py`,
  `src/gobby/storage/hub/postgres.py`,
  `src/gobby/events/wake.py`,
  `src/gobby/agents/agent_cleanup.py`,
  `src/gobby/runner_lifecycle_agents.py`,
  `src/gobby/runner_gate.py`,
  `src/gobby/runner_lifecycle.py`. test:
  `tests/storage/test_storage_agents.py`,
  `tests/storage/hub/test_runtime_pool_config.py`,
  `tests/events/test_wake.py`,
  `tests/agents/test_agent_cleanup.py`,
  `tests/test_runner_lifecycle.py`.

## P2: Wrapper simplification
`kind: framing`

**Goal**: the wrapper treats `wait_for_agent` as an ordinary fast tool.

### 2.1 Remove wait_for_agent from wrapper wait-tool handling [category: code] (depends: 1.2)
`kind: deliverable`

Target: `src/gobby/mcp_proxy/wait_tools.py`,
`src/gobby/mcp_proxy/stdio_proxy.py`

Remove `"wait_for_agent"` from `WAIT_TOOL_NAMES` (`wait_tools.py:13-19`). That
single removal drops, for this tool: the 300s arg rewrite in
`prepare_client_guard`, the 15s heartbeat, the 5s grace guard in
`_await_with_guard`, the 30s HTTP buffer and special `/api/mcp/tools/call`
routing in `DaemonProxy.call_tool` (`stdio_proxy.py:248-313`), and the
wait-tool source-staleness guards. The tool then uses the ordinary per-server
route and default 30s HTTP timeout, which the immediate-return contract fits.
No other entries in `WAIT_TOOL_NAMES` or `EXTENDED_TIMEOUT_TOOL_NAMES` change
(cap drift for the rest is #18516).

Tests that use `wait_for_agent` as the exemplar wait tool switch to
`wait_for_summary` (the only remaining implemented wait tool) so wrapper
coverage is retained: `tests/mcp_proxy/test_wait_tools.py` (heartbeat guard),
`tests/mcp_proxy/test_mcp_proxy_stdio.py` (buffer, routing, heartbeat,
staleness, wrapper-timeout), `tests/mcp_proxy/test_gobby_daemon_tools.py`
(coordinator wait paths). Add an explicit test that `wait_for_agent` receives
no wait guard, no heartbeat, no timeout rewriting, and no special route.
`tests/events/test_mcp_tool_changes.py::test_wait_for_agent_is_in_agents_registry`
stays valid and unchanged.

**Acceptance:**

- 2.1.1 - `WAIT_TOOL_NAMES` no longer contains `wait_for_agent`; remaining
  entries unchanged. file: `src/gobby/mcp_proxy/wait_tools.py`.
- 2.1.2 - `wait_for_agent` gets no wait guard, heartbeat, timeout rewrite, or
  special routing; wrapper wait-tool tests retained via another blocking wait
  tool. test: `tests/mcp_proxy/test_mcp_proxy_stdio.py`.
- 2.1.3 - Daemon-side nested call path likewise treats it as ordinary. test:
  `tests/mcp_proxy/test_gobby_daemon_tools.py`.

## P3: Guidance, workflows, and docs
`kind: framing`

**Goal**: every bundled instruction surface teaches subscribe-once, end the
turn, full sweep after the daemon wake; behavioral workflow rules keep
working.

### 3.1 Rework merge orchestration for wake-driven waits [category: config] (depends: 1.2)
`kind: deliverable`

Target: `src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml`,
`src/gobby/install/shared/skills/merge-expert/SKILL.md`,
`src/gobby/install/bundled_content_manifest.json`

This is behavioral, beyond doc text: `merge-orchestrator.yaml:413-430` has
three `on_mcp_success` rules keyed on `tool: wait_for_agent` that consume
`tool_output.completed` / `run_id` (flat and nested) to set
`merge_worker_completed`, `post_worker_merge_status_checked`, and
`last_worker_run_id`. `on_mcp_success` rules fire only on MCP tool
completions — the completion wake is an ISM plus nudge, not an MCP event —
so the wake itself cannot update workflow state. The wake-resume transition
is therefore defined explicitly:

- **State capture at spawn.** Add an `on_mcp_success` rule on
  `tool: spawn_agent` setting `last_worker_run_id` from the spawn output's
  `run_id` (alongside the existing spawn rules at `:403-412` that reset
  `merge_worker_completed` / `post_worker_merge_status_checked`), so the
  worker's run id is persisted before any wait.
- **Terminal status read after wake.** The dispatch→wait→proceed flow
  becomes: dispatch worker → call `wait_for_agent(run_id)` once (active run:
  `completed: false`, no state rule fires) → end the turn → on the daemon
  wake, **re-call `wait_for_agent(run_id)`** as the first step of the status
  sweep. The run is now terminal, so the call returns the terminal snapshot
  immediately and the existing `completed == true` rules fire and derive
  `merge_worker_completed` / `last_worker_run_id` through the normal MCP
  hook sequence. `get_agent_result` stays optional for re-reading the final
  report only.
- **Batch dispatch contract.** The orchestrator's `allowed_mcp_tools`
  includes `gobby-agents:dispatch_batch` (`:386`), whose output carries
  per-suggestion `results[].{run_id, success}` entries, and its internal
  Python spawns fire no per-run `spawn_agent` MCP success hooks — so
  neither the spawn-time capture rule nor the spawn-time flag resets above
  cover the parallel branch. Add `on_mcp_success` rules on
  `tool: dispatch_batch`: reset `merge_worker_completed` and
  `post_worker_merge_status_checked` to false (mirroring the `spawn_agent`
  reset rules at `:403-412`, which batch dispatch currently bypasses — a
  stale `merge_worker_completed=true` left by a prior worker would
  otherwise satisfy the `inspect_merge_state` gates immediately), and set
  a `current_batch_run_ids` **list** variable to the successful non-empty
  run ids. Real `mcp__gobby__call_tool` hook output reaches rules
  normalized by `_unwrap_mcp_tool_output` to the proxy
  `structuredContent` envelope, whose semantic payload stays nested as
  `{success: true, result: {dispatched, results}}`
  (`hooks/_normalization_mcp.py:61-99`) — the same dual-shape reason
  the existing wait rules read flat and nested `run_id` — so the
  capture rule normalizes first, appending the nested `result`'s
  successful ids (flat/native fallback) to whatever is already
  outstanding, so a follow-up batch never clobbers live state:
  `"(vars.get('current_batch_run_ids') or []) + [r.get('run_id') for r in ((tool_output.get('result') or tool_output).get('results') or []) if r.get('success') and r.get('run_id') and r.get('run_id') not in (vars.get('current_batch_run_ids') or [])]"`.
  List-typed rule variables are established practice in this workflow
  (`verification_evidence` list append, `:496-497`) and
  `SafeExpressionEvaluator` evaluates comprehensions
  (`safe_evaluator.py:362-387`). Failed entries (`success` false or empty
  `run_id`) never enter batch state; their re-dispatch goes through
  `dispatch_batch` again — append semantics make that safe mid-batch,
  while `spawn_agent` is blocked below whenever ids are outstanding.
  Flow: after
  `dispatch_batch`, call `wait_for_agent(run_id)` once per captured id
  (each registers a subscription), then end the turn. Each completion
  delivers its own wake whose ISM payload carries `run_id` (§1.3). On each
  wake, re-call `wait_for_agent(<woken run_id>)` first — its terminal
  payload fires the existing `completed == true` rules and re-derives
  `merge_worker_completed` / `last_worker_run_id` — and a new rule on the
  same terminal payload removes the woken id from the batch:
  `"[r for r in (vars.get('current_batch_run_ids') or []) if r != str(tool_output.get('run_id') or tool_output.get('result', {}).get('run_id'))]"`.
  That first wake still sets the single global
  `merge_worker_completed=true` through the shared rules, so batch
  completion is never derived from that boolean alone — every
  consumer of it gains the outstanding-batch condition: the three
  `inspect_merge_state` rules (`:431-448`) and the `merge_status`
  no-progress derivation (`:459-463`) each require
  `not vars.get('current_batch_run_ids')` before recording state,
  and the `on_mcp_before` `spawn_agent` block (`:536-548`) gains an
  OR branch on `bool(vars.get('current_batch_run_ids'))` with
  batch-specific reason text, so while any batch id remains
  outstanding no merge-state inspection outcome is recorded, no
  no-progress signal derives, and no replacement worker spawns. The
  single-worker flow is untouched by the gates — its
  `current_batch_run_ids` is empty or absent, and only the
  dispatch-capture append and the terminal-wake removal ever change
  the list.
  The outstanding set is therefore durable workflow state that survives
  turn boundaries and repeated wakes idempotently (removing an absent id
  is a no-op; re-calling `wait_for_agent` on an active run re-subscribes
  idempotently via registry merge + ISM dedup), and the post-wake sweep
  re-calls `wait_for_agent` for every id still in
  `current_batch_run_ids`. `list_agent_runs` (limit-capped, no batch
  identity) is a diagnostic cross-check only, never the recovery
  mechanism. The instruction text (`:80`, "dispatch_batch for
  parallel-safe steps") is updated to teach this per-run subscribe /
  per-wake process-then-sweep flow.

Update instructions (lines 80, 82, 170) and `allowed_mcp_tools` (line 387)
to the subscribe-and-end-turn contract, and update merge-expert
SKILL.md:187 accordingly. Update the consuming contract tests. Regenerate
the bundled manifest.

**Acceptance:**

- 3.1.1 - Orchestrator instructions and rules reflect subscribe-once /
  wake-then-re-call; `on_mcp_success` rules derive `merge_worker_completed` /
  `last_worker_run_id` from the post-wake terminal `wait_for_agent` payload;
  `last_worker_run_id` is additionally captured at spawn; `dispatch_batch`
  rules reset both completion flags and append to `current_batch_run_ids`;
  the terminal-payload rule removes the woken id from the batch list; and
  the `inspect_merge_state` rules, the no-progress derivation, and the
  `spawn_agent` block gate all carry the outstanding-batch condition. file:
  `src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml`.
- 3.1.2 - merge-expert guidance drops bounded-wait language for
  subscribe-and-end-turn plus post-wake re-call. file:
  `src/gobby/install/shared/skills/merge-expert/SKILL.md`.
- 3.1.3 - Contract tests updated for the new output contract and instruction
  text. test: `tests/agents/test_merge_orchestrator_contract.py`.
- 3.1.4 - Two-turn active-path scenario exercising the real hook sequence:
  turn one dispatches (spawn rules set `last_worker_run_id` and reset the
  completion flags) and calls `wait_for_agent` (`completed: false` — no
  state rule fires, variables stay unset) and ends; turn two simulates the
  post-wake `wait_for_agent` re-call whose terminal `completed: true` payload
  flows through `on_mcp_success` dispatch and sets `merge_worker_completed`
  and `last_worker_run_id`. test:
  `tests/agents/test_merge_orchestrator_contract.py`.
- 3.1.5 - Two-turn batch scenario exercising the real hook sequence,
  starting from stale prior-worker state (`merge_worker_completed=true`,
  `post_worker_merge_status_checked=true`): turn one calls
  `dispatch_batch` returning two successes and one failed entry —
  exercised in **both** output shapes, the flat/native
  `{results: [...]}` payload and the real nested proxy envelope
  `{success, result: {dispatched, results}}` as it leaves the hook
  normalizer (`_unwrap_mcp_tool_output`), asserting both shapes
  capture the same two ids — the batch rules reset both flags to
  false and capture exactly the two
  successful run ids in `current_batch_run_ids` — then per-run
  `wait_for_agent` subscribe calls (`completed: false`, no state rules
  fire) and ends; turn two processes two wakes in sequence: each terminal
  `wait_for_agent` re-call fires the `completed == true` rules, sets
  `last_worker_run_id` to the woken run, and removes that id from
  `current_batch_run_ids`, which shrinks to one id after the first wake
  (surviving the turn boundary) and to empty after the second, while a
  sweep re-call for a still-active run returns `completed: false` and
  leaves all state untouched; between the two wakes — with
  `merge_worker_completed` already true and one id outstanding — an
  `inspect_merge_state` call records no state, the no-progress
  derivation stays false, and `spawn_agent` is blocked, and after the
  final wake empties the list the same inspection records state and
  spawn is no longer batch-blocked. test:
  `tests/agents/test_merge_orchestrator_contract.py`.

### 3.2 Update coordinator, goal, and plan guidance [category: config] (depends: 3.1)
`kind: deliverable`

Target: `src/gobby/install/shared/skills/build-coordinator/SKILL.md`,
`src/gobby/install/shared/skills/goal/SKILL.md`,
`src/gobby/install/shared/skills/plan/SKILL.md`,
`src/gobby/install/shared/workflows/agents/goal-taskmaster.yaml`,
`src/gobby/install/bundled_content_manifest.json`

Replace bounded-wait idle guidance (build-coordinator:108; goal:209, 329;
plan:100-102, which pins `wait_for_agent(run_id, timeout_seconds=300)` and
prescribes bounded re-waits; goal-taskmaster:81) with: when workers are
running and nothing is actionable, call `wait_for_agent(run_id)` once to
subscribe, end the turn, and perform a full status/health sweep after the
daemon wake (re-calling `wait_for_agent` for the terminal snapshot). Keep the
compaction-before-waits ordering contract. This is the complete bundled
consumer set — a shared-tree sweep found `wait_for_agent` guidance only in
these four surfaces plus the two §3.1 surfaces; there is no
`skills/agents/SKILL.md` (removed historically — do not recreate it). Update
pinned-content tests (`tests/skills/test_build_coordinator_skill.py:120-139`
pins the literal "five-minute wait (`timeout_seconds=300`)" text and the
ordering test), the delegated-mode plan-skill pins
(`tests/skills/test_plan_skill_delegated_mode.py`), and the TDD-harness
scenario
(`tests/skills/scenarios/build-coordinator/unattended-build-coordination.yaml`).
Extend `tests/skills/test_removed_wait_tool_guidance.py` (precedent from the
`wait_for_completion` removal) to assert `timeout_seconds` is absent from the
updated skill bodies. Regenerate the bundled manifest; this leaf runs after
3.1 so the manifest is written serially, never in parallel.

**Acceptance:**

- 3.2.1 - All four bundled surfaces teach subscribe-once / end-turn /
  sweep-on-wake with no timeout parameters. file:
  `src/gobby/install/shared/skills/build-coordinator/SKILL.md`.
- 3.2.2 - Build-coordinator content pins updated, compaction-before-waits
  ordering test still passes. test:
  `tests/skills/test_build_coordinator_skill.py`.
- 3.2.3 - Plan-skill delegated-mode pins updated to the subscribe-and-end-turn
  contract. file: `src/gobby/install/shared/skills/plan/SKILL.md`. test:
  `tests/skills/test_plan_skill_delegated_mode.py`.
- 3.2.4 - Removed-guidance absence assertions cover the updated skills. test:
  `tests/skills/test_removed_wait_tool_guidance.py`.
- 3.2.5 - Bundled manifest regenerated and parity holds. test:
  `tests/test_build_backend.py::test_committed_bundled_content_manifest_matches_shared_tree`.
- 3.2.6 - The updated TDD-harness scenario
  (`tests/skills/scenarios/build-coordinator/unattended-build-coordination.yaml`)
  passes under its owner harness test. test:
  `tests/skills/test_skill_tdd_harness.py`.

### 3.3 Update MCP tool documentation [category: docs] (depends: 1.2)
`kind: deliverable`

Target: `docs/guides/mcp-tools.md`

Update the `wait_for_agent` row (line 690, currently "wrapper-capped
timeout") to the event-driven contract: returns current status immediately,
registers a durable completion notification for active runs, wake arrives via
inbox message plus live nudge.

**Acceptance:**

- 3.3.1 - Reference table and any surrounding prose describe the
  subscribe-and-return contract with no timeout language. file:
  `docs/guides/mcp-tools.md`.

## V2 End-to-End Verification
`kind: verification`

- Focused protected test files (never the full suite), with
  `GOBBY_TEST_PROTECT=1`: `tests/mcp_proxy/tools/test_agents.py`,
  `tests/mcp_proxy/tools/test_agent_live_stats.py`,
  `tests/agents/test_completion_subscribers.py`,
  `tests/agents/test_agent_cleanup.py`,
  `tests/agents/test_capture.py`,
  `tests/agents/test_run_completion.py`,
  `tests/events/test_subscriber_storage.py`,
  `tests/workflows/test_agent_workflow_completion.py`,
  `tests/events/test_wake.py`, `tests/events/test_wake_wiring.py`,
  `tests/events/test_completion_registry.py`,
  `tests/hooks/test_session_coordinator.py`,
  `tests/mcp_proxy/tools/test_agent_cancellation.py`,
  `tests/storage/test_storage_agents.py`,
  `tests/storage/test_database_executor.py`,
  `tests/storage/hub/test_runtime_pool_config.py`,
  `tests/test_runner_init.py`,
  `tests/agents/test_lifecycle_monitor.py`,
  `tests/dispatch/test_dispatcher.py`,
  `tests/dispatch/test_daemon_resume.py`,
  `tests/scheduler/test_cron_executor.py`,
  `tests/build/test_build_stop.py`,
  `tests/servers/websocket/test_resume_blocked.py`,
  `tests/servers/routes/test_agent_spawn_routes.py`,
  `tests/mcp_proxy/tools/spawn_agent/test_health.py`,
  `tests/mcp_proxy/tools/spawn_agent/test_error_handling.py`,
  `tests/agents/test_resume_executor.py`,
  `tests/servers/routes/test_agents_routes.py`,
  `tests/servers/routes/test_admin_extended.py`,
  `tests/cli/test_cli_agents.py`, `tests/cli/test_agents_coverage.py`,
  `tests/test_runner_lifecycle.py`, `tests/test_runner_pid_file.py`,
  `tests/test_runner_shutdown.py`, `tests/mcp_proxy/test_wait_tools.py`,
  `tests/mcp_proxy/test_mcp_proxy_stdio.py`,
  `tests/mcp_proxy/test_gobby_daemon_tools.py`,
  `tests/servers/test_mcp_routes.py`,
  `tests/agents/test_merge_orchestrator_contract.py`,
  `tests/skills/test_build_coordinator_skill.py`,
  `tests/skills/test_plan_skill_delegated_mode.py`,
  `tests/skills/test_removed_wait_tool_guidance.py`,
  `tests/skills/test_skill_tdd_harness.py`,
  `tests/events/test_mcp_tool_changes.py`, `tests/test_build_backend.py`.
- `uv run ruff check src/` and `uv run mypy src/` on touched modules.
- Live check against an isolated test daemon: spawn a short agent, call
  `wait_for_agent(run_id)` from the parent session, confirm immediate
  `completed: false` + `notification_registered: true` return, then confirm
  the completion ISM lands in the parent inbox and the live nudge fires;
  confirm a second call after completion returns the terminal payload with no
  new subscription and that `completion_subscribers` has no rows for the run.

## V1 Plan Changelog
`kind: verification`

**Round 0** `kind: verification`

- reviewer_run: (pre-adversary draft)
- verdict: pending
- findings: incorporated interactive review — wrapper-layer cap correction,
  strict-persistence split, normal-terminal cleanup folded in (was a known
  blocker), merge-orchestrator behavioral rules, content-pin test inventory.
- resolution_notes: initial draft from approved interactive feedback;
  adversary review not yet run.

**Round 1** `kind: enhancement`

- reviewer_run: b2191ff7-e65d-4d44-b9d8-1780e51dd027 (plan-enhancer-taskless,
  codex)
- verdict: converged: false (4 suggestions, all verified against code and
  accepted by user)
- findings: E1 strict mode must persist durable rows before in-memory
  registration (live-only subscriber on failure otherwise); E2 §1.3 baseline
  was stale — `post_terminal_cleanup` already cleans up, real leak is
  `complete_and_notify_agent_run` bypass callers (`end_agent_run`
  self-termination, enforcement fallback); E3 `AgentsRegistryContext` already
  has all needed fields, context extension dropped; E4 added two-turn
  active-path orchestrator scenario (3.1.4).
- resolution_notes: all four applied — Overview leak framing corrected, §1.1
  persist-first ordering + 1.1.2 extended, §1.2 context reuse +
  `notification_session_id` assertion, §1.3 rewritten around bypass paths
  with `docs/reviews/agents.md` correction, §3.1 gained 3.1.4, V2 gained
  `tests/workflows/test_agent_workflow_completion.py`.

**Round 2** `kind: adversary`

- reviewer_run: 067219c5-00c3-437f-b548-8c90f49e632c (plan-adversary-taskless,
  codex xhigh, adversary review round 1)
- verdict: needs_review (5 blocking findings; artifact unmodified by
  reviewer)
- findings: F1 strict registration paired with lossy terminal delivery
  (`notify` discards wake results, `ism_persist_failed` swallowed, cleanup
  sweeps undelivered rows, bare `{"status": ...}` payloads defeat dedup);
  F2 completion-race branch globally deleted shared registration, losing
  pre-existing subscribers' wakes when terminalization pauses between status
  write and notify; F3 two-turn merge flow could never set
  `merge_worker_completed` — wake is not an MCP event and no handler matched
  the sweep tools; F4 consumer sweep missed `plan/SKILL.md` +
  `test_plan_skill_delegated_mode.py`, targeted nonexistent
  `skills/agents/SKILL.md`, and left `docs/reviews/agents.md:207-211` stale;
  F5 expansion ownership ambiguous (shared `test_agents.py` and manifest
  writers parallel, phase-level `depends: P1` invalid in M1, unchanged
  caller files listed as 1.1 targets).
- resolution_notes: all five verified against code and addressed — F1: new
  §1.3 (acknowledged terminal delivery: `run_id` injected centrally in
  `notify_terminal_completion`, `notify` returns delivery map, delivered-only
  row removal, deliver-then-remove startup sweep); F2: §1.2 race branch now
  ownership-aware via `created_fresh_entry` from 1.1.3 (fresh entry ⇒ sole
  owner, safe cleanup; merged entry ⇒ left for canonical
  notify→cleanup cycle), deterministic pause-terminalization concurrency test
  added (1.2.9); F3: §3.1 defines the wake-resume transition — spawn-time
  `last_worker_run_id` rule + mandatory post-wake `wait_for_agent` re-call so
  existing `completed == true` rules fire on the real hook sequence, 3.1.4
  rewritten; F4: §3.2 targets swap `agents/SKILL.md` → `plan/SKILL.md`, add
  delegated-mode pins, complete-sweep statement, `:207-211` correction folded
  into §1.4 (1.4.5); F5: old §1.3 renumbered §1.4 with `depends: 1.2, 1.3`
  (serializes `test_agents.py` and cleanup-test ownership), §1.3 depends on
  §1.2 (serializes `test_wake_wiring.py`), §3.2 depends on §3.1 (serializes
  manifest writes, manifest listed as owned target), all phase-level
  `depends: P1` replaced with deliverable IDs; §1.1 targets keep the two
  direct consumer files (consumer-sweep contract requires them — validated)
  with explicit no-edit framing, and drop only the indirect
  `dispatch/spawn.py` re-export.

**Round 3** `kind: adversary`

- reviewer_run: ce2fc703-d3a0-4554-a388-e4767ab2fc00 (plan-adversary-taskless,
  codex xhigh, adversary review round 2)
- verdict: needs_review (5 blocking findings; artifact unmodified by
  reviewer)
- findings: F1 (terminal-ack-producer-sweep) the cleanup-follows-awaited-
  notify invariant is false — `SessionCoordinator.complete_agent_run`
  fire-and-forgets `notify` with no cleanup, `agent_cancellation.py`
  no-monitor paths notify directly, killed-error payload lacks `run_id`,
  neither file in the sweep; F2 (fresh-entry-race-invariant)
  `created_fresh_entry` does not prove no pending notify — real safety is
  owning-loop confinement + a no-await critical region, unstated and
  untested (1.2.9 covered only the merged branch); F3
  (startup-redelivery-ack) sweep's unconditional remove deletes the only
  retry state when redelivery fails again, and the post-failure liveness
  contract was unstated; F4 (merge-batch-wake-resume) `dispatch_batch`
  remains allowed but the wake-resume transition was defined only for
  singular `spawn_agent`; F5 (wake-ack-test-surface) `ism_persisted` is
  load-bearing yet `tests/events/test_wake.py` was absent from acceptance
  and V2.
- resolution_notes: all five verified against code and addressed — F1:
  Overview producer inventory expanded; new §1.3 shared awaited helper
  `deliver_and_cleanup_terminal_run` (inject `run_id` → await notify →
  delivered-only row removal → registry cleanup) with defined
  no-delivery-map behavior (remove nothing, cleanup still idempotent);
  §1.4 gains `session_coordinator.py` + `agent_cancellation.py` targets,
  routes all three bypass producer groups through the helper
  (`_notify_agent_completion` schedules the helper chain so ordering holds
  without blocking the hook thread), acceptance 1.4.6 (coordinator
  ordering, `tests/hooks/test_session_coordinator.py`) and 1.4.7
  (no-monitor cancel/error, `tests/mcp_proxy/tools/
  test_agent_cancellation.py`); F2: false rationale replaced with the
  owning-loop no-await critical-region invariant (stated in the sketch
  comment as a shipped-code requirement), fresh-branch removal scoped to
  the caller's lineage via a `session_ids` filter moved into §1.1
  (`pipeline_subscribers.py` now a 1.1 target, acceptance 1.1.4), new
  fresh-branch paused-terminalization test 1.2.10 (resumes notify after
  cleanup, asserts no-op on unregistered ID); F3: sweep is acknowledged
  deliver-then-remove — rows removed only on `ism_persisted` or
  `session_not_found`, failed rows retained for the next restart's sweep,
  liveness contract stated explicitly as a restart-triggered retry with no
  timers, 1.3.4 extended with the fail→fail→succeed chain; F4: §3.1 gains
  the batch contract (per-run subscribe, per-wake process-then-sweep,
  wake-payload `run_id` + `list_agent_runs` recovery instead of list-typed
  state) and two-turn batch test 3.1.5; F5: acceptance 1.3.6 pins the
  `WakeDispatcher.wake` result contract in `tests/events/test_wake.py`,
  1.3.5 requires real dispatcher results through the delivered map, V2
  gains the three new test files. §1.3 targets also list all seven direct
  `notify`/wiring consumers as consumer-sweep no-edit entries, serialized
  with §1.4's edits via the existing depends chain.

**Round 4** `kind: adversary`

- reviewer_run: 0ec19b0f-0e8a-467e-b484-2be204194174 (plan-adversary-taskless,
  codex xhigh, adversary review round 3)
- verdict: needs_review (4 blocking findings; artifact unmodified by
  reviewer)
- findings: R3-F1 (§1.2) the no-await proof started too late — the awaited
  `overlay_live_activity` sat between the first status read and
  registration, so a notify could snapshot subscribers mid-await and a
  later merge missed delivery; `created_fresh_entry` proves only in-memory
  ownership because `add_completion_subscribers` is `ON CONFLICT DO
  NOTHING`, so fresh-branch full-lineage deletion could erase pre-existing
  retained retry rows; 1.2.9/1.2.10 orderings hit the initial terminal
  fast path and never exercised either race branch. R3-F2 (§1.3) the
  delivered-map contract was not total — `wake_callback=None` is legal
  (`completion_registry.py:39`) and `WakeCallback` returns unconstrained
  `object`, so a missing callback or unknown result had no specified
  delivered value and an optimistic implementation could delete the sole
  retry row. R3-F3 (§3.1) `dispatch_batch` bypasses the `spawn_agent`
  state-reset rules (stale `merge_worker_completed=true` satisfies the
  `inspect_merge_state` gates), the "rules-engine variables are scalar"
  rationale is contradicted by this workflow's own list variables
  (`verification_evidence`, `:496-497`), and `list_agent_runs` (limit 20,
  no batch identity) cannot deterministically reconstruct the outstanding
  batch. R3-F4 (§1.4/V2) `run_completion.py`'s owner test
  `tests/agents/test_run_completion.py` — which asserts a direct `notify`
  await with a `run_id`-less payload — was absent from the section and V2
  despite the helper routing invalidating its contract.
- resolution_notes: all four verified against code and addressed — R3-F1:
  §1.2 sketch restructured so the terminal fast path returns before any
  registration concern, both overlay awaits sit outside the critical
  region, and the region opens with a fresh status re-read (the last read
  that can observe active) followed by registration, the hook-thread-race
  re-read, and conditional cleanup, all synchronous; fresh-branch cleanup
  now keys on durable-row ownership via `inserted_session_ids` — §1.1's
  `add_completion_subscribers` switches to `INSERT ... ON CONFLICT DO
  NOTHING RETURNING session_id` and reports created rows (new acceptance
  1.1.5, `tests/events/test_subscriber_storage.py`); post-sketch prose
  records both failed prior rationales; 1.2.9/1.2.10 rewritten as
  overlay-paused orderings (late-notify snapshot; completed-notify with
  retained-row preservation) and new 1.2.11 covers the in-region
  hook-thread transition for both fresh and merged branches via a stubbed
  `get_run` sequence. R3-F2: §1.3's classifier is now total and
  conservative — delivered only on `ism_persisted` true (insert or dedup)
  or `session_not_found`; missing callback, raised exception,
  `None`/non-mapping result, and `ism_persisted`-less mapping all map to
  undelivered with the row retained; 1.3.2 pins the classifier cases and
  1.3.7 covers the helper's per-case row retention. R3-F3: §3.1 adds
  `on_mcp_success` rules on `dispatch_batch` that reset both completion
  flags and capture successful `results[].run_id` values into a
  `current_batch_run_ids` list variable (comprehension support verified,
  `safe_evaluator.py:362-387`; list-variable precedent `:496-497`), plus a
  terminal-payload rule that removes the woken id, making the outstanding
  set durable, idempotent workflow state; `list_agent_runs` demoted to
  diagnostic cross-check; 3.1.5 rewritten to start from stale flags,
  include a failed batch entry, and process two wakes across the turn
  boundary. R3-F4: §1.4 states the `complete_and_notify_agent_run` notify
  step becomes the awaited helper and its owner test's direct-notify
  assertion is updated (new acceptance 1.4.8);
  `tests/agents/test_run_completion.py` added to V2.

**Round 5** `kind: adversary`

- reviewer_run: cb7a0dac-16de-454c-87b9-bf577dfdf3a8 (plan-adversary-taskless,
  codex xhigh, adversary review round 4)
- verdict: needs_review (1 blocking finding; artifact unmodified by
  reviewer)
- findings: R4-F1 (§1.2) the mandated critical-region invariant was
  factually incomplete — it claimed the hook thread is the only status
  writer that can interleave with the no-await region, but
  `AgentCleanupHandler` terminalizers perform complete/cancel/fail/timeout
  transitions through `_run_db` on worker threads
  (`agent_cleanup.py:365-442`, `444-522`, `541-632`) and
  `complete_and_notify_agent_run` performs `runner.complete_run` through
  `asyncio.to_thread` (`run_completion.py:16-46`); those writes interleave
  with the synchronous region too, so the plan required shipping a false
  load-bearing comment and scoped 1.2.11 as a hook-thread-only
  simulation.
- resolution_notes: verified against code (`_run_db` defaults to
  `asyncio.to_thread`, `agent_cleanup.py:97-100`; `cleanup_agent` commits
  complete/timeout/fail via `_run_db` before awaiting
  `notify_terminal_completion`; `complete_and_notify_agent_run` awaits
  `asyncio.to_thread(runner.complete_run)` before awaiting
  `completion_registry.notify`) and addressed: the sketch's INVARIANT
  comment now names the full off-owner-loop writer set (hook thread,
  `_run_db` worker-thread terminalizers, `asyncio.to_thread`
  complete_run) and states the shared ordering fact — every such writer
  commits its DB transition before its notify/cleanup work is scheduled
  or resumes on the owning loop, so the post-registration re-read catches
  any interleaved transition and its notify cannot snapshot until the
  region exits; the post-sketch proof records the third failed rationale
  ("hook thread is the only interleaving writer") alongside the writer
  inventory with file/line citations and mandates the comment carry the
  full writer set and ordering; the in-sketch race comment generalized
  from "hook-thread transition race" to "off-loop transition race";
  acceptance 1.2.11 generalized from a hook-thread simulation to a
  stand-in for any off-owner-loop transition writer, retaining both the
  fresh and merged branches.

**Round 6** `kind: adversary`

- reviewer_run: f60bafa5-cef0-44d7-a841-36b4a8f05ac6 (plan-adversary-taskless,
  codex xhigh, adversary review round 5)
- verdict: needs_review (2 blocking findings; artifact unmodified by
  reviewer; validation passed)
- findings: R5-F1 (§1.2 invariant and 1.2.11) the claimed full
  off-owner-loop terminal-writer inventory was still incomplete —
  `capture_then_kill_async` runs `_default_terminalize` through
  `_async_storage_call` (= `asyncio.to_thread`, `capture.py:339-344`,
  `459-472`) when no terminalize callback is supplied,
  `_close_tmux_session` supplies none (`kill.py:296-319`), and
  `kill_agent` reaches that path for active tmux runs with
  `close_terminal=true` (`kill.py:341-585`), so the mandated shipped
  comment enumerating "exactly" three writer groups remained factually
  false. R5-F2 (Overview, §1.2, §1.3/§1.4 sweep)
  `AgentLifecycleMonitor._check_loop` awaits
  `_run_db(cleanup_stale_runs)` (`lifecycle_monitor.py:325-330`; `_run_db`
  defaults to `asyncio.to_thread`, `:216-219`), and `cleanup_stale_runs`
  (`storage/agents/_cleanup.py:29-117`) transitions matching running rows
  to timeout returning only a count — no notify or acknowledged cleanup
  follows, so a `wait_for_agent` caller that already returned
  `notification_registered=true` is stranded until daemon restart,
  violating the loss-free liveness contract; the writer was also absent
  from the §1.2 inventory and the three-producer-group claim.
- resolution_notes: both verified against code, and the R5-F2 sweep the
  finding demanded surfaced more: the restart sweep pre-plan only deletes
  terminal-run rows (never delivers), `cleanup_stale_pending_runs` has
  the same no-notify shape with an every-heartbeat dispatcher caller
  (`dispatcher.py:195-197`, outside the startup block) plus
  cleanup-handler/startup callers, three `kill_agent(close_terminal=True)`
  callers issue no follow-up notify (websocket observe-continue,
  `cleanup_unattached_spawned_run`, build stop's `_cancel_active_agents`
  — whose monitor branch skips notify via the `transitioned_here` guard,
  `agent_cleanup.py:481-489`), `unregister_agent` cancels with no notify
  (`agents_query_tools.py:296-304`), spawn/resume failure paths fail runs
  bare, and `gobby agents cleanup` terminalizes out-of-process. Fixes:
  R5-F1 — per the reviewer's preferred resolution, the shipped INVARIANT
  comment now states the commit-before-notify ordering property plus a
  pointer to the §1.4 terminal-producer contract instead of any writer
  enumeration ("exactly" claims went stale in two consecutive rounds);
  the §1.2 proof paragraph records the fourth failed rationale with the
  capture-default-terminalizer citations and reworded mandate (c);
  1.2.11's stand-in list gains the capture-policy default terminalizer.
  R5-F2 — §1.4 now opens with the terminal-producer contract (every
  `agent_runs` terminal transition enters the acknowledged delivery
  chain exactly once or carries a code-proven exclusion), inventories
  both bypass classes with citations, converts the sweeps
  (`cleanup_stale_runs`/`cleanup_stale_pending_runs` return transitioned
  ids; all in-daemon callers await `deliver_and_cleanup_terminal_run`
  per id), routes kill-path/unregister/failure producers through the
  helper, gates the CLI's direct-DB mode on daemon unreachability with
  next-boot sweep delivery, and documents the shutdown-cancel and
  missing-tmux exclusions; new acceptance 1.4.9-1.4.11 (waiter-liveness
  before the sweep, per-site helper routing with idempotent duplicate
  delivery, CLI gating chain), 1.4.3's mode list extended, §1.3 names
  the sweep's designed clients, Overview reframed around the two bypass
  classes, V2 gains the eleven affected test files.

**Round 7** `kind: adversary`

- reviewer_run: ebeaee9e-781d-4d2c-ab40-f517d87db25a (plan-adversary-taskless,
  codex xhigh, adversary review round 6)
- verdict: needs_review (3 blocking findings; artifact unmodified by
  reviewer; validation passed)
- findings: R6-F1 (§1.4 inventory) the claimed exhaustive terminal-writer
  inventory omitted `POST /api/agents/runs/{run_id}/cancel` —
  `create_agents_router.cancel_agent_run` kills the process and then
  `_reconcile_cancelled_agent_run` calls `LocalAgentRunManager.cancel`
  directly (`servers/routes/agents.py:40-55`, `717-744`) with no notify,
  helper, or next-boot-only exclusion, stranding a registered waiter;
  `tests/servers/routes/test_agents_routes.py` was absent from §1.4 and
  V2. R6-F2 (§1.4 inventory) the sweep also omitted
  `register_testing_routes`: `register_test_agent` can
  complete/cancel/fail/timeout an `agent_runs` row directly
  (`servers/routes/admin/_testing.py:119-189`) and
  `unregister_test_agent` directly fails an existing — possibly active
  and subscribed — run (`:192-235`); test-mode-only is not itself a
  code-proven no-subscriber exclusion. R6-F3 (§1.3/§1.4 shutdown
  exclusion) the shutdown proof was factually wrong:
  `_register_persisted_completion_subscribers` reads existing
  `completion_subscribers` rows and registers them in memory — it never
  persists rows (`runner_lifecycle_agents.py:16-33`, `238-273`); the
  true safety argument is that strict `wait_for_agent` registration
  persisted the rows earlier, and shutdown branches between a
  `terminalize_cancelled_run` live delivery and a prior capture-policy
  transition that leaves the rows for the next-boot sweep.
- resolution_notes: all three verified against code
  (`cancel_agent_run` kills with default `close_terminal=False` then
  commits via bare `manager.cancel`, retried once, nothing notifies;
  both admin endpoints commit terminal transitions through direct
  `LocalAgentRunManager` calls in their own transactions, so a strict
  registration can land in the create→terminalize window;
  `_register_persisted_completion_subscribers` is read-and-register
  only). Fixes: R6-F1 — `servers/routes/agents.py` added to §1.4
  targets and the never-notify inventory; the route awaits
  `deliver_and_cleanup_terminal_run` after
  `_reconcile_cancelled_agent_run` returns, covering both the
  route-committed transition and an already-terminal reconcile; new
  acceptance 1.4.12 pins pre-registered-waiter wake, acknowledged row
  cleanup, failed-delivery retention, and idempotent already-terminal
  reconciliation in `tests/servers/routes/test_agents_routes.py`, added
  to V2. R6-F2 — `servers/routes/admin/_testing.py` added to targets
  and inventory; both endpoints routed through the helper after any
  terminal write (routed rather than argued as exclusions — the
  fresh-row registration window is real because create and terminalize
  commit separately); new acceptance 1.4.13 in
  `tests/servers/routes/test_admin_extended.py`, added to V2. R6-F3 —
  every "persists rows before killing" claim replaced (§1.3 designed-
  clients bullet, §1.4 exclusions paragraph) with the branch-complete
  proof: strict registration persisted the rows, shutdown never deletes
  them, `transitioned_here` true delivers live through the helper while
  the capture-policy branch leaves untouched rows for the next-boot
  acknowledged sweep; new acceptance 1.4.14 pins the
  shutdown-to-restart chain for both branches in
  `tests/test_runner_lifecycle.py`. 1.4.3's mode list and the Overview
  producer list gained the HTTP cancel route and test-mode admin
  endpoints.

**Round 8** `kind: adversary`

- reviewer_run: 8c548196-83c8-42d3-adc2-2a25a21a6d37 (plan-adversary-taskless,
  codex xhigh, adversary review round 7)
- verdict: needs_review (3 blocking findings; artifact unmodified by
  reviewer; validation passed)
- findings: R7-F1 (§1.2 / §1.4 inventory) the inventory missed a second
  runtime capture-policy default terminalizer —
  `_kill_spawned_tmux_session` calls `capture_then_kill_async` with no
  `terminalize` callback (`resume_executor.py:394-432`), so
  `_default_terminalize` writes error status off-thread
  (`capture.py:452-472`); `resume_agent_run` reaches it on the
  runtime-persist-failure and start-skipped branches
  (`resume_executor.py:236-264`), and tmux cases do not pass through
  the inventoried `_fail_run` — falsifying §1.2's claim that
  `_close_tmux_session` is the sole runtime callback omission. R7-F2
  (§1.4 / 1.4.12) the cancel-route fix did not specify exception-safe
  helper placement: the route commits cancellation in a `finally` even
  when `kill_agent` raises (`servers/routes/agents.py:734-737`), so a
  helper awaited after that try/finally is skipped on the re-raise
  although terminal state already committed. R7-F3 (§1.3/§1.4 shutdown
  proof / 1.4.14) the corrected proof was internally false — "shutdown
  never deletes durable rows" cannot coexist with the helper contract:
  the `transitioned_here` branch runs `terminalize_cancelled_run`
  (`runner_lifecycle_agents.py:266-270`), whose chain invokes
  `post_terminal_cleanup` (`agent_cleanup.py:494-509`), and the planned
  helper removes acknowledged rows by design.
- resolution_notes: all three verified against code
  (`_kill_spawned_tmux_session` passes no `terminalize`, and both
  `resume_agent_run` failure branches skip `_fail_run` whenever a tmux
  session was spawned; the route's `finally` commits the reconcile on
  the kill-exception path before the re-raise; `terminalize_cancelled_
  run` notifies then runs `post_terminal_cleanup`, which removes
  subscriber rows). Fixes: R7-F1 — §1.2's sole-call-site sentence
  corrected to both omitting call sites and 1.2.11's stand-in list
  extended; §1.4 inventory gains the resume capture-policy kill-helper
  bullet (both branches, `_fail_run` skipped for tmux cases, swallowed
  policy exceptions); the Fix paragraph routes both branches through a
  post-kill re-read — terminal row → helper with the 1.3
  sweep-synthesized payload, still-active row → `_fail_run` plus its
  helper routing — and 1.4.10 pins acknowledged cleanup,
  failed-delivery retention, and the active-row fallback for these
  exact branches in `tests/agents/test_resume_executor.py`. R7-F2 —
  the Fix paragraph and 1.4.12 mandate the reconcile and the awaited
  helper in one `finally` chain (commit first, delivery second, before
  any kill exception propagates), with the raising-`kill_agent` route
  case added to 1.4.12. R7-F3 — the shutdown proof rewritten around
  delete-after-acknowledgement in both statements (§1.3
  designed-clients bullet, §1.4 exclusions paragraph): the live branch
  removes only delivered-map-acknowledged rows (`ism_persisted` or
  `session_not_found`) and retains ISM-persist failures for startup
  replay, while the capture-policy branch performs no cleanup and
  leaves all rows for the next-boot sweep; 1.4.14 extended with
  live-success removal and live-failure retention.

**Round 9** `kind: adversary`

- reviewer_run: dbb69830-e0ac-4253-b592-42ba79d5b9e8
  (plan-adversary-taskless, codex xhigh, adversary review round 8)
- verdict: needs_review (4 blocking findings; artifact unmodified by
  reviewer; validation passed)
- findings: R8-F1 (§1.4 inventory / 1.4.7 / 1.4.10) the "MCP kill tool
  is NOT in this class" parenthetical was false: `stop_agent_run` kills
  with `close_terminal=True` before `terminalize_cancelled_agent_run`,
  the capture default terminalizer can commit the cancellation first,
  and both terminalize branches no-op on the already-terminal row with
  no notify (`agent_cancellation.py:53-92,175-184`); MCP
  `kill_agent(stop=False)` with default `debug=False` returns before
  `terminalize_killed_agent_run` entirely
  (`agents_lifecycle_tools.py:248-272`), and
  `agents_lifecycle_tools.py` was absent from targets and 1.4.10.
  R8-F2 (§1.4 / 1.4.12) the sequential same-`finally` chain was not
  exception-safe for reconciliation itself: the reconcile's unguarded
  retry or `manager.get` can raise after `LocalAgentRunManager.cancel`
  committed terminal state (`servers/routes/agents.py:40-55`,
  `storage/agents/_lifecycle.py:321-351`), skipping delivery exactly as
  a kill exception did. R8-F3 (§1.4 spawn/resume routing / 1.4.10)
  helper delivery was mandated without the wiring that supplies the
  live registry: `AgentRunner` exposes none (`runner.py:40-93`),
  `try_resume_daemon_stop_run` passes none into `resume_agent_run`
  (`daemon_resume.py:40-77`), and the spawn factory owns
  `completion_registry` but `spawn_agent_impl` and the deferred health
  check receive none (`_factory.py:463-510`,
  `_implementation.py:157-192,886-904`, `_health.py:81-139`); the
  caller files were not targets. R8-F4 (§1.3/§1.4 CLI exclusion /
  1.4.11) the reachability probe was not exclusive with daemon
  startup: the daemon can start, load subscribers, and finish its
  startup sweep between the CLI's probe and its direct-DB terminal
  writes (`cli/agents.py:826-862`, `runner_lifecycle_agents.py:36-51`),
  stranding a strict waiter on an already-terminal row that no later
  sweep revisits.
- resolution_notes: all four verified against code (both terminalize
  branches skip notify on already-terminal rows and `kill_agent`
  returns pre-terminalize when `stop=False`; `cancel` writes the
  terminal row before `_expire_sessions_for_run_ids` and `get`;
  `services.completion_registry` exists — `spawn_completion.py:61`
  reads it — but is threaded to none of these paths; the CLI non-dry-
  run mode calls `cleanup_stale_runs` directly against the DB). Fixes:
  R8-F1 — inventory parenthetical replaced with the capture-preempted
  MCP-surface statement; `agents_lifecycle_tools.py` added to targets;
  the Fix paragraph routes both surfaces through a post-terminalize
  re-read → helper (`kill_agent(stop=False)` keeps its
  no-explicit-terminalize semantics and delivers any capture-committed
  transition before returning); 1.4.7 extended with
  pre-registered-waiter, acknowledged-cleanup, and failed-delivery-
  retention cases for both surfaces; 1.4.3's mode list gains
  capture-preempted MCP stop/kill. R8-F2 — the cancel route now
  mandates a nested chain (reconcile in `try`, terminal re-read plus
  awaited helper in `finally`) in the Fix paragraph and 1.4.12, with
  the reconcile-raises and raising-`get` cases pinned and a failed
  re-read/delivery leaving rows retained for the sweep. R8-F3 —
  registry threading specified end-to-end (spawn factory →
  `spawn_agent_impl` → health check and failure cleanup; dispatch
  resume services → `try_resume_daemon_stop_run` →
  `resume_agent_run`); `_factory.py`, `_implementation.py`, and
  `dispatch/daemon_resume.py` added to targets; 1.4.10 gains the
  wiring statement plus a real-registry integration case in
  monitor-present and no-monitor wiring, with
  `tests/dispatch/test_daemon_resume.py` added to 1.4.10 and V2.
  R8-F4 — the destructive offline mode is removed outright: non-dry-run
  routes through a new `POST /api/agents/cleanup` endpoint running the
  acknowledged in-daemon sweeps and errors when the daemon is
  unreachable, closing the concurrent-start race by construction;
  1.4.11 rewritten accordingly.

**Round 10** `kind: adversary`

- reviewer_run: 585fc59a-a1f8-475b-acf7-840318861937
  (plan-adversary-taskless, codex xhigh, adversary review round 9)
- verdict: needs_review (4 blocking findings; artifact unmodified by
  reviewer; validation passed)
- findings: R9-F1 (§1.4 inventory / 1.4.7) the round-9 MCP routing
  omitted the ordinary `kill_agent(stop=True, debug=False)` path:
  `agents_lifecycle_tools.py:249-265` continues into
  `terminalize_killed_agent_run`, whose cancelled branch (no
  transition) and error branch (`run_storage.fail` returns `None` when
  capture already committed) only debug-log the no-op
  (`agent_cancellation.py:95-151`) — the generic "capture-preempted
  MCP stop/kill" claim was broader than the stated routing and tests.
  R9-F2 (§1.4 registry threading / 1.4.10) spawn wiring covered only
  the factory although `spawn_agent_impl` is called directly by
  `dispatch/spawn.py:203-205`, `scheduler/executor.py:326-328`, and
  `servers/routes/agent_spawn.py:339-341`, none of them targets — a
  direct-surface spawn can return a `run_id`, gain a waiter, then have
  the deferred health check terminalize with no registry in scope.
  R9-F3 (§1.4 websocket routing / 1.4.10) the observe-continue helper
  delivery was unimplementable from the stated wiring:
  `_release_source_session` receives only the mixin
  (`session_observe_continue.py:38-77`), `SessionControlMixin`
  declares no registry (`session_control.py:32-58`),
  `WebSocketServer.__init__` accepts none (`websocket/server.py:71-166`),
  and `runner_init/servers.py:129-147` passes none. R9-F4
  (Overview / §1.3 / traceability) normative text still named
  out-of-process CLI cleanup as a terminal producer (Overview) and a
  designed client of the terminal-row startup sweep (§1.3),
  contradicting round 9's removal of destructive offline CLI writes.
- resolution_notes: all four verified against code (`kill_agent`
  stop=True flows into both silent no-op branches; the three direct
  `spawn_agent_impl` callers exist with a live `ServiceContainer`
  handle in scope at each site — `executor.services` is assigned at
  `runner_init/servers.py:81`; the WebSocketServer construction site
  already has `services` in scope but passes no registry; both stale
  CLI passages found). Fixes: R9-F1 — the Fix paragraph now routes
  every non-self MCP kill independently of `stop`:
  `terminalize_killed_agent_run` routes both no-transition branches
  (cancelled and error) through the terminal re-read plus awaited
  helper; `debug=True` (no capture preemption; canonical explicit
  terminalize) and self-termination (1.4.1) pinned; the inventory
  bullet documents the stop=True no-op pair; 1.4.7 extended with
  stop=True/debug=False waiter cases for both request shapes plus the
  companion `debug=True` and self-termination pins. R9-F2 —
  `spawn_agent_impl` takes the registry as an explicit parameter
  supplied by every direct caller (dispatch spawn, cron executor, HTTP
  spawn route — each via its owning `services.completion_registry`);
  the three caller files added to targets; 1.4.10 gains the
  per-surface deferred-health-failure waiter cases with
  `tests/scheduler/test_cron_executor.py` and
  `tests/servers/routes/test_agent_spawn_routes.py` added there and to
  V2. R9-F3 — registry source specified end-to-end: `init_servers`
  passes `runner.completion_registry` into `WebSocketServer.__init__`,
  stored on `SessionControlMixin`, read by `_release_source_session`
  for the post-kill re-read plus helper; registry-less construction
  performs no delivery and retains rows for the startup sweep;
  `websocket/server.py`, `session_control.py`, and
  `runner_init/servers.py` added to targets; a websocket
  pre-registered-waiter case added to 1.4.10. R9-F4 — the Overview
  producer list drops the CLI; §1.3's designed-client list shrinks to
  shutdown cancel and states the two-stage daemon-down chain (CLI
  performs no writes; next boot's stale sweeps terminalize in-daemon
  and route through acknowledged delivery, 1.4.9); historical
  changelog text preserved as history.

**Round 11** `kind: adversary`

- reviewer_run: 88298ab4-9147-4ebe-a88a-003be7e80d08
  (plan-adversary-taskless, codex xhigh, adversary review round 10)
- verdict: needs_review (3 blocking findings; artifact unmodified by
  reviewer; validation passed)
- findings: R10-F1 (§1.4 inventory / 1.4.11 traceability) the normative
  Out-of-process CLI producer bullet still said the CLI runs both
  stale-run cleanup writers directly against the DB, contradicting the
  same section's Fix and 1.4.11, which route non-dry-run cleanup
  through the daemon endpoint with no direct-DB terminal writes.
  R10-F2 (§1.4 Fix / 1.4.7 / 1.4.10 unhandled edge) the
  post-terminalize/no-transition routing missed early exits after a
  close-terminal kill already capture-committed: `agents/kill.py` calls
  `_close_tmux_session` first (`:371-381`) and can then return
  `success=False` (e.g. "Terminal closed but no target PID was found to
  verify process death", `:473-487`); `stop_agent_run` and MCP
  `kill_agent` return such failures before terminalize/re-read
  (`agent_cancellation.py:177-188`, `agents_lifecycle_tools.py:249-264`)
  and `cleanup_unattached_spawned_run` returns before its fail/re-read
  path (`spawn_actions.py:189-207`), stranding a registered waiter on
  ordinary failure-result branches. R10-F3 (§1.4 registry threading /
  1.4.10 sequencing) `cleanup_unattached_spawned_run` was promised an
  awaited helper but had no live registry source: it accepts only
  `run_id`/`db`/`error` (`spawn_actions.py:178-183`), the dispatcher
  wrapper forwards only those (`dispatcher.py:451-457`), and the
  `services` handle stops at `execute_spawn_action`.
- resolution_notes: all three verified against code (the stale CLI
  bullet stood at the inventory's tail; the kill-failure return
  "Terminal closed but no target PID" carries no
  `KILL_ERROR_NO_TARGET_PID`, so both MCP surfaces short-circuit on it
  after `_close_tmux_session` succeeded; `execute_spawn_action` holds
  `services` but `_cleanup_or_quarantine_spawned_run` and the wrapper
  drop it). Fixes: R10-F1 — the inventory bullet now opens "removed as
  a producer class by this plan", documents today's direct-DB writes as
  the pre-plan state, and states the two-stage daemon-down chain
  (no CLI terminal write in any daemon state; next boot's in-daemon
  sweeps terminalize and enter acknowledged delivery, 1.4.9/1.4.11);
  the old claim survives only in changelog history. R10-F2 — the MCP
  kill clause now places post-kill routing ahead of **every** exit:
  `stop_agent_run` and both `stop` values of `kill_agent` perform the
  terminal re-read plus awaited helper before their kill-failure
  returns as well as on the no-transition branches;
  `kill_agent(stop=False)` re-reads on both its exits; the inventory
  documents the shared kill-failure early return and
  `cleanup_unattached_spawned_run`'s failure exits; 1.4.7 gains the
  kill-result-failure waiter case for all three surfaces; 1.4.10's
  dispatch-cleanup case pins kill-failure and caught-kill-exception
  delivery. R10-F3 — registry threading extended: `execute_spawn_action`
  passes `services.completion_registry` through
  `_cleanup_or_quarantine_spawned_run` and the dispatcher wrapper into
  `cleanup_unattached_spawned_run` (new keyword-only parameter), which
  re-reads and awaits the helper before every exit; a `None` registry
  performs no delivery and retains the durable rows; the wiring chain
  and a real-registry pre-registered-waiter dispatch-cleanup case added
  to 1.4.10 (`tests/dispatch/test_dispatcher.py`).

**Round 12** `kind: adversary`

- reviewer_run: e6de02ee-4f5c-4df8-8c4e-eb4e13670d5e
  (plan-adversary-taskless, codex xhigh, adversary review round 11)
- verdict: needs_review (3 blocking findings; artifact unmodified by
  reviewer; validation passed)
- findings: R11-F1 (§1.3/§1.4 contract unhandled edge) terminal
  transition APIs can raise after the terminal row committed:
  `complete`/`fail`/`timeout`/`cancel` commit their UPDATE through
  `HubDatabase.execute` (own transaction,
  `storage/hub/postgres.py:375-385`) and then run
  `_expire_sessions_for_run_ids` and `get` post-commit
  (`storage/agents/_lifecycle.py:192-351`), so a post-write failure
  strands a committed terminal row behind an exception — the routed
  producers re-read only after terminalize returns, and
  `cleanup_stale_runs` can lose transitioned ids when `self.timeout`
  raises mid-loop. R11-F2 (§3.1 shape mismatch) the `dispatch_batch`
  capture rule read only flat `tool_output.results`, but real
  `call_tool` hook output is normalized to the proxy envelope with
  the payload nested under `result`
  (`hooks/_normalization_mcp.py:61-99`), so production capture would
  store `[]` while a flat-only 3.1.5 fake passed. R11-F3 (§3.2
  traceability) the §3.2-required TDD-harness scenario edit
  (`unattended-build-coordination.yaml`) had no owning acceptance
  item and its consumer (`tests/skills/test_skill_tdd_harness.py:32`)
  was missing from V2.
- resolution_notes: all three verified against code
  (`PostgresHubDatabase.execute` opens and commits its own
  transaction per call with both post-write steps as separate DB
  statements; `_unwrap_mcp_tool_output` returns the
  `structuredContent` envelope whose payload stays under `result`;
  the harness test consumes the scenario at
  `test_skill_tdd_harness.py:32`). Fixes: R11-F1 — new Fix step
  zero: all four terminal methods wrap UPDATE + session expiry + row
  re-read in one reentrant `self.db.transaction()` (ambient-join,
  `storage/hub/_ambient.py:28-70`), so each returns the transitioned
  run, returns `None` on the already-terminal guard, or raises with
  nothing committed; indeterminate commit outcomes fall to the
  durable-row/next-boot backstop; `cleanup_stale_runs` gains per-id
  try/except isolation (bulk-UPDATE `cleanup_stale_pending_runs`
  needs none); the contract paragraph, cancel-route justification,
  and 1.4.12 updated to the atomic premise; `_lifecycle.py` added to
  targets; new 1.4.15 (injected post-UPDATE failures prove rollback)
  and a 1.4.9 per-id isolation clause. R11-F2 — the capture
  comprehension normalizes first:
  `(tool_output.get('result') or tool_output).get('results')`, with
  the envelope shape documented; 3.1.5 now exercises both the
  flat/native and nested-envelope shapes and asserts identical
  captured ids. R11-F3 — new 3.2.6 pins the updated scenario under
  `tests/skills/test_skill_tdd_harness.py`, which is added to V2.

**Round 13** `kind: adversary`

- reviewer_run: d34d34be-310c-4be9-bb01-275b916d6286
  (plan-adversary-taskless, codex xhigh, adversary review round 12)
- verdict: needs_review (1 blocking finding; artifact unmodified by
  reviewer; validation passed)
- findings: R12-F1 (§1.4 step zero / §1.2 invariant unhandled edge)
  the claimed reentrant composition does not hold inside an existing
  ambient transaction: `enter_transaction` merely yields the existing
  `Transaction` with no savepoint and no rollback-only marking
  (`storage/hub/_ambient.py:29-56`), so an outer caller could catch a
  post-UPDATE fault and still commit the terminal UPDATE at outer
  exit — falsifying "raises ⇒ nothing committed" — and on the nested
  success path the method returns before the outer commit, letting
  delivery be scheduled against an uncommitted terminal row and
  breaking the 1.2 commit-before-delivery invariant.
- resolution_notes: verified against code (`enter_transaction`'s
  reentrant branch yields the existing transaction and returns; no
  savepoint API exists on the ambient path) and the caller sweep
  confirmed no production producer invokes the four terminal methods
  inside an ambient transaction (the only `db.transaction()` blocks
  in caller modules are in unrelated functions:
  `build/controls.py:855`, `_delete_child_session` in
  `spawn_agent/_failure_cleanup.py:126`, and the bulk sweep UPDATE in
  `storage/agents/_cleanup.py:127`). Fix — adopted the reviewer's
  least-mechanism option: the four terminal methods refuse ambient
  nesting, raising `TerminalTransitionNestedError(RuntimeError)` via
  an `ambient_transaction(self.db)` check before the UPDATE, so the
  step-zero transaction is always outermost and the three-outcome
  contract (returns transitioned run committed-before-return /
  returns `None` / raises with nothing committed) holds
  unconditionally; step-zero paragraph rewritten to the prohibition
  boundary with the caller-sweep evidence; new acceptance 1.4.16
  (nested call raises without executing the UPDATE, run stays
  non-terminal after the outer commit; un-nested call transitions
  normally), owner test `tests/storage/test_storage_agents.py`.

**Round 14** `kind: adversary`

- reviewer_run: baee0c18-0fdb-4910-aa3e-05caf710323e
  (plan-adversary-taskless, codex xhigh, adversary review round 13)
- verdict: needs_review (1 blocking finding; artifact unmodified by
  reviewer; validation passed)
- findings: R13-F1 (§1.4 kill-path routing; 1.4.7/1.4.10 correctness)
  the MCP stop/kill and websocket observe-continue placements covered
  post-kill failure returns but not exceptions raised after the
  capture-policy terminalizer committed: `kill_agent` can return
  successfully from `_close_tmux_session` (transition committed) and
  then raise — the process-group signal path catches only
  `ProcessLookupError` (`kill.py:531-543`) so `PermissionError`
  escapes, and the TERM wait/escalation path (`kill.py:545-565`) can
  raise through the signal-0 probe, identity re-check, or SIGKILL
  escalation — while `stop_agent_run` and non-self MCP `kill_agent`
  await the kill bare (`agent_cancellation.py:175-188`,
  `agents_lifecycle_tools.py:248-264`) and the websocket handler
  converts the exception to `RuntimeError`
  (`session_observe_continue.py:47-57`), so a committed terminal row
  could strand with no delivery attempt until daemon restart.
- resolution_notes: verified against code (`_close_tmux_session`
  catches its own exceptions, so a successful return can follow a
  committed `terminate_managed_tmux_async` transition;
  `_signal_process_group` is `os.killpg` with only
  `ProcessLookupError` caught and runs post-commit whenever the
  signal is not TERM; `_wait_for_pid_exit` probes `os.kill(pid, 0)`
  catching only `ProcessLookupError`; both MCP surfaces and the
  websocket handler lack exception-path routing). Fix: the MCP kill
  routing now mandates exception-safe wraps — `stop_agent_run` and
  both `stop` values of non-self `kill_agent` perform the terminal
  re-read plus awaited helper on the kill-exception path before the
  exception propagates (`stop=False` still never explicitly
  terminalizes), and `_release_source_session` runs the same re-read
  plus helper before its converted `RuntimeError` propagates; 1.4.7
  gains deterministic kill-stub-commits-then-raises cases for all
  three MCP shapes and 1.4.10 a websocket companion case, each
  pinning acknowledged cleanup and failed-delivery retention.

**Round 15** `kind: adversary`

- reviewer_run: f456e755-a72e-49a3-b469-2fa2a61854ee
  (plan-adversary-taskless, codex xhigh, adversary review round 14)
- verdict: needs_review (1 blocking finding; artifact unmodified by
  reviewer; validation passed)
- findings: R14-F1 (§1.2/§1.4 terminal-producer contract;
  1.4.7/1.4.9/1.4.10/1.4.15 unhandled-edge) async cancellation was
  an uncovered committed-terminal escape: every async producer
  reaches the storage methods through unshielded `asyncio.to_thread`
  bridges (`agent_cleanup.py:97-100`, `lifecycle_monitor.py:216-219`,
  `run_completion.py:16-46`, `capture.py:339-344`), and once the
  worker has started, cancelling the awaiting task raises
  `asyncio.CancelledError` — a direct `BaseException` subclass on
  Python 3.13 that every planned `except Exception` wrap misses —
  while the worker commits the terminal UPDATE; cancellation is
  production-reachable (`AgentLifecycleMonitor.stop` cancels its
  loop task, `lifecycle_monitor.py:289-299`; daemon shutdown cancels
  live HTTP/MCP request tasks,
  `runner_lifecycle_shutdown.py:211-226`), so a committed row could
  strand undelivered until restart even after the Round-14
  exception wraps.
- resolution_notes: verified against code (all four bridges are bare
  `await asyncio.to_thread(...)`; the websocket boundary catches
  only `Exception`; monitor stop and the shutdown drain cancel live
  tasks). Fix: new cancellation-shielded delivery scope
  (`shielded_terminal_delivery`, beside the 1.3 helper) — the
  transition offload plus terminal re-read plus awaited helper run
  in an owned task; the caller awaits through `asyncio.shield`,
  waits out settlement under repeated cancellation, then
  re-raises — routed through `complete_and_notify_agent_run`, the
  AgentCleanup terminalizers, both stale-sweep caller chains, and
  every direct sync-transition-then-helper placement, with the
  kill-surface wraps upgraded to `BaseException` semantics and the
  websocket `RuntimeError` conversion bypassed for `CancelledError`;
  capture's `_async_storage_call` terminalize offload becomes an
  owned settled task; step zero extended with the fourth
  offload-boundary guarantee; acceptance 1.4.7/1.4.8/1.4.9/1.4.10
  gain deterministic commits-then-cancel cases and new 1.4.17 pins
  the scope semantics; `capture.py` added to §1.4 Targets and
  `tests/agents/test_capture.py` to V2.

**Round 16** `kind: adversary`

- reviewer_run: 7fd7330e-697e-455c-ad3a-dd101fa0a7f1
  (plan-adversary-taskless, codex xhigh, adversary review round 15)
- verdict: needs_review (2 blocking findings; artifact unmodified by
  reviewer; validation passed)
- findings: R15-F1 (§1.4 shielded scope / 1.4.9, 1.4.11
  unhandled-edge) the "exhaustive" shielded stale-sweep caller
  inventory named four callers while 1.4.11 introduces a fifth — the
  `POST /api/agents/cleanup` route that runs both sweeps
  (`storage/agents/_cleanup.py:29-117`, `:119-161`; CLI today at
  `cli/agents.py:826-862`) inside an HTTP request task that daemon
  shutdown cancels (`runner_lifecycle_shutdown.py:211-226`) — so a
  post-worker-start cancellation could commit terminal rows and skip
  the route's per-id acknowledged delivery, with no
  commits-then-cancel case covering it. R15-F2 (§1.4 producer
  routing / 1.4.6 unhandled-edge) `SessionCoordinator` stayed
  outside `shielded_terminal_delivery`: after the synchronous
  hook-thread terminal commit, `_notify_agent_completion`
  fire-and-forgets the helper coroutine — discarding the Task/Future
  from `create_task` / `run_coroutine_threadsafe`
  (`hooks/session_coordinator.py:741-770`) — with no owner to wait
  out a loop-shutdown interruption and no documented next-boot
  exclusion, and 1.4.6 tested only ordering.
- resolution_notes: verified against code (both sweeps, the CLI
  direct invocation, the fire-and-forget scheduling at all four
  `complete_agent_run` notify sites, and the shutdown request-task
  cancel). R15-F1: the sweep-and-deliver chain is now one shared
  acknowledged-sweep operation, `run_acknowledged_stale_sweeps`, on
  the cleanup handler — sweep offload(s) plus per-id helper loop in
  one shielded delivery scope — and all five in-daemon callers,
  the cleanup route included, invoke it; the route joined the
  inventory in the scope paragraph, the never-notify Fix, 1.4.9, and
  1.4.11, and 1.4.9 gains the route commits-then-cancel case (route
  test files added). R15-F2: the coordinator's scheduled delivery is
  documented as the one producer outside the scope, with a
  retention-based code-proven next-boot exclusion — the chain
  removes durable rows only on acknowledged delivery, so task
  cancellation at loop shutdown, a hand-off onto a closing loop, or
  the no-loop skip each retain every row for the next-boot
  acknowledged sweep, and only loop shutdown can cancel the
  loop-parented task; 1.4.6 gains deterministic cancellation cases
  for both scheduling branches plus a settled-delivery companion.

**Round 17** `kind: adversary`

- reviewer_run: 527517e4-04b5-4ed7-9858-fb5e534455b8
  (plan-adversary-taskless, codex xhigh, adversary review round 16)
- verdict: needs_review (2 blocking findings; artifact unmodified by
  reviewer; validation passed)
- findings: R16-F1 (§1.3 startup redelivery / 1.4.6 unhandled-edge)
  the claimed next-boot exclusion was not end-to-end: the acknowledged
  terminal-row sweep stayed inside `_recover_agent_runs_after_restart`
  (`runner_lifecycle_agents.py:57-60`), whose only production startup
  caller runs behind `_start_agent_lifecycle_monitor`'s
  monitor-is-None early return
  (`runner_lifecycle_subsystems.py:383-390`; monitor construction
  degrades to None, `runner_init/orchestration.py:181-200`), and the
  sweep no-ops when optional `pipeline_execution_manager` is absent
  (`runner_lifecycle_agents.py:36-43`; pipeline init is fail-open)
  while strict subscriptions write rows directly through
  `CompletionSubscriberManager(db)`
  (`agents/completion_subscribers.py:75-82`) — so a committed
  coordinator terminal row could retain subscribers yet be skipped on
  the next boot, and §1.3 declared the startup-wiring file
  consumer-only/no-edit. R16-F2 (§1.4 shielded scope / 1.4.17
  weak-testability) acceptance 1.4.17 required
  cancellation-before-worker-start to commit nothing, while the
  primitive transfers the offload to an owned task that keeps running
  under `asyncio.shield` after caller cancellation; production
  `DatabaseExecutor.run` submits to a bounded pool and awaits the
  queued future (`storage/executor.py:61-69`), so a queued worker
  cannot be revoked without contradicting the keep-running semantics —
  the acceptance case was incompatible with the planned
  implementation.
- resolution_notes: verified against code (sweep call site and its
  monitor-gated caller chain, the pipeline-manager no-op, fail-open
  pipeline and degrade-to-None monitor init, direct-from-db strict
  persistence, and the executor submit-then-await bridge). R16-F1:
  the acknowledged sweep is now an unconditional startup step —
  `init_subsystems` invokes it directly before
  `_start_agent_lifecycle_monitor`, independent of
  `AgentLifecycleMonitor` and `pipeline_execution_manager`, with
  storage handles constructed directly from the database and the old
  gated call site dropped; `runner_lifecycle_subsystems.py` moved
  from consumer-only to edited in §1.3, 1.3.4 gains deterministic
  next-boot cases with monitor None and pipeline manager None, and
  both retention-exclusion passages plus 1.4.6 now cite the
  unconditional sweep. R16-F2: the scope contract is pinned at one
  boundary — scope entry: cancellation before invoking the scope
  performs no transition work; cancellation after ownership transfer
  waits out the owned transition and acknowledged
  delivery/retention settlement even while the executor worker is
  still queued, and a submitted offload is never revoked; the fourth
  offload-boundary guarantee, the primitive text, and 1.4.17 were
  rewritten to that contract and the pre-worker-start no-commit
  assertion removed.

**Round 18** `kind: adversary`

- reviewer_run: 662f4fe8-e080-412f-99bc-325cf8c184e0
  (plan-adversary-taskless, codex xhigh, adversary review round 17)
- verdict: needs_review (2 blocking findings; artifact unmodified by
  reviewer; validation passed)
- findings: R17-F1 (§1.3 / 1.3.4 bad-sequencing) the round-17
  "unconditional" sweep was still sequenced immediately before
  `_start_agent_lifecycle_monitor`, after every operation at
  `runner_lifecycle_subsystems.py:803-817`; `init_subsystems` runs as
  a background task (`runner_lifecycle.py:226-232`) whose done
  callback only logs an uncaught failure
  (`runner_lifecycle_startup.py:48-66`), and earlier optional
  operations can raise — `_check_external_services` has no outer
  exception boundary around its awaited probes and graph-client
  mutation (`runner_lifecycle_subsystems.py:116-149`) — so an
  exception in any of them leaves the daemon serving while retained
  terminal rows are never replayed on that boot; the monitor-None and
  pipeline-None cases did not cover this remaining gate. R17-F2
  (§1.4 fourth guarantee / primitive / 1.4.17 unhandled-edge) the
  post-scope-entry promise that a submitted `DatabaseExecutor.run`
  offload is never revoked was false under production shutdown:
  `_shutdown_database_executor` calls
  `shutdown(wait=False, cancel_futures=True)`
  (`runner_lifecycle_shutdown.py:542-552`), cancelling queued
  unstarted futures, while request-task cancellation and monitor stop
  are bounded (`runner_lifecycle_shutdown.py:211-226`, `:424-445`) —
  `asyncio.shield` protects against caller-task cancellation, never
  against executor-level future cancellation, so a shield-owned
  transition could still sit queued when executor shutdown revoked
  its future and the queued-worker commit assertion in 1.4.17 could
  not hold on the real shutdown path.
- resolution_notes: verified against code (the init_subsystems
  operation sequence and its background-task launch with log-only
  done callback, the boundary-free `_check_external_services` body,
  `_shutdown_database_executor`'s cancel_futures call and its
  position in `_run_async_shutdown_cleanup` ahead of
  `runner.database.close()` in `shutdown_daemon_services`' finalizer,
  and both bounded graceful-phase waits). R17-F1: the sweep is now
  the first executable operation in `init_subsystems`, ahead of
  `_schedule_provider_model_refresh` and every optional
  health/service initialization, with the fallibility rationale
  stated in §1.3; 1.3.4 gains the raising-next-startup-operation
  case proving the retained row replayed before the failure
  surfaces, and every retention-exclusion citation (§1.4 scope
  paragraph, coordinator passage, 1.4.6) now cites the
  first-operation sweep. R17-F2: the never-revoked claim is scoped
  to the scope itself and to a serving daemon; §1.4 gains
  `runner_lifecycle_shutdown.py` as an edited target and a
  shutdown-drain design — owned tasks tracked in a module-level
  in-flight set, `drain_shielded_terminal_deliveries` awaited in
  `_run_async_shutdown_cleanup` before executor revocation and
  database close, deadline-expiry shape argued through step-zero
  atomicity — pinned by new acceptance 1.4.18 with a saturated
  executor, a queued owned transition, producer cancellation, and
  shutdown ordering proven end-to-end.

**Round 19** `kind: adversary`

- reviewer_run: 6831f7f5-7ad4-43d7-9a2e-9b09301b2ca3
  (plan-adversary-taskless, codex xhigh, adversary review round 18)
- verdict: needs_review (3 blocking findings; artifact unmodified by
  reviewer; validation passed)
- findings: R18-F1 (§1.3 / 1.3.4 / 1.4.4 unhandled-edge) the
  first-operation step replayed only terminal runs; durable
  subscribers for still-active runs stayed gated behind optional
  services — `_start_agent_lifecycle_monitor` returns before
  reconciliation when the monitor is None
  (`runner_lifecycle_subsystems.py:383-390`),
  `_recover_agent_runs_after_restart` requires `agent_runner` and
  `completion_registry` and reads durable rows only through
  `pipeline_execution_manager` (`runner_lifecycle_agents.py:57-92`),
  both fail-open in `runner_init/orchestration.py` — so a strict
  durable subscription could survive a restart on an active run,
  receive no registry re-registration, and strand when the run
  terminalized on the serving daemon after the terminal-only sweep
  had already passed, with no periodic replay until another restart.
  R18-F2 (§1.4 drain / 1.4.18 bad-sequencing) a one-time snapshot of
  the in-flight set is not a closed-set drain: graceful shutdown
  bounds request-task cancellation and monitor stop, and the deferred
  spawn health checks (module-level `_health_check_tasks` in
  `_health.py`, `cancel_health_checks` with no production caller) are
  an unmanaged late producer whose `_deferred_tmux_health_check` can
  enter a terminal scope after the snapshot and overlap executor
  shutdown or database close. R18-F3 (§1.4 deadline argument /
  1.4.18 bad-sequencing) the deadline-expiry claim assumed revocation
  before database loss, but `_shutdown_database_executor` ran inside
  the overall `asyncio.timeout_at` block: on expiry before cleanup or
  during the drain, executor shutdown is skipped while the outer
  finally closes `runner.database`, so queued or running executor
  work could outlive database close with no revocation at all.
- resolution_notes: verified against code (the monitor-None early
  return and pipeline-gated subscriber read, the unconditional
  completion-registry and WakeDispatcher construction ahead of every
  fail-open init block at `runner_init/orchestration.py:139-148`, the
  free-running health-check tasks with caller-less
  `cancel_health_checks`, and `shutdown_daemon_services`' timeout
  block with database close in its finally at
  `runner_lifecycle_shutdown.py:762-766`). R18-F1: the first
  executable operation is now a two-step boot recovery — active-run
  subscriber rehydration through the directly-constructed
  `CompletionSubscriberManager` into the unconditionally-built
  registry, then the acknowledged sweep — with rehydration ordered
  first so a boot-concurrent transition lands via live notify or the
  subsequent sweep; the pipeline-gated subscriber loading is dropped,
  reconciliation's residual registration is pinned idempotent, the
  Overview and periodic-sweep passages re-attributed, 1.4.4 restated
  for the restructuring, and new 1.3.8 pins monitor-None and
  pipeline-None active-run delivery with no second restart. R18-F2:
  the graceful phase gains bounded cancel-and-await producer
  quiescence for the deferred health checks, an admission boundary
  beside the in-flight set closes before the drain — post-boundary
  invocations perform no transition work and resolve through the
  daemon-down two-stage chain — and the drain loops to a stably
  empty set; 1.4.18 rewritten with the late-second-scope,
  post-admission, and quiescence cases. R18-F3:
  `_shutdown_database_executor` relocates into
  `shutdown_daemon_services`' finalizer immediately ahead of
  `runner.database.close()`, unconditional on every exit shape;
  revoked queued workers settle as retention and empty the tracking
  set, started workers reach commit-or-rollback via step-zero
  transaction atomicity; new 1.4.19 pins all three shapes.

**Round 20** `kind: verification`

- reviewer_run: bff7fe8a-781a-49ef-87f9-296c12a6f34b
- verdict: needs_review (adversary review round 19; 2 blocking
  findings; artifact unmodified by reviewer; validation passed)
- findings: R19-F1 (blocking, bad-sequencing, §1.4/1.4.18-1.4.19):
  the deadline finalizer revokes only `runner.db_executor` work
  while two shield-routed terminal producers submit storage
  transitions to asyncio's loop-default executor —
  `complete_and_notify_agent_run`'s `asyncio.to_thread` offload
  (`run_completion.py:27-31`) and capture's `_async_storage_call`
  (`capture.py:339-344`) — and `_shutdown_database_executor`
  controls only the custom executor
  (`runner_lifecycle_shutdown.py:542-552`,
  `storage/executor.py:105-111`), so deadline-mid-drain
  default-executor work survives revocation and races
  `runner.database.close()`. R19-F2 (blocking, unhandled-edge,
  §1.4/1.4.19): the started-worker commit-or-rollback proof assumed
  pool close terminates in-use connections, but
  `PostgresHubDatabase.close` delegates to `ConnectionPool.close`
  (`storage/hub/postgres.py:483-491`) and psycopg_pool leaves
  checked-out connections open until returned
  (`psycopg_pool/pool.py:427-455`), so under wait=False a started
  worker could commit after `runner.database.close()` and after the
  pid claim released (`runner_lifecycle.py:138-143`,
  `runner_lifecycle_shutdown.py:762-773`), racing the replacement
  daemon's recovery sweep and stranding durable rows past the next
  boot.
- resolution_notes: both findings verified against code — the
  to_thread bridges, the executor-scoped shutdown, the psycopg_pool
  close docstring, and the finally ordering all held as cited.
  R19-F1: a module-level terminal-offload seam beside
  `shielded_terminal_delivery` (default bare bridge, daemon init
  points it at the managed executor's `run`) now carries
  `complete_and_notify_agent_run`'s offload and every
  `_async_storage_call` site; the coordinator's hook-thread commit
  joins the choke point via a new sync `submit` on
  `DatabaseExecutor`, with revoked or refused submissions
  committing nothing and resolving through the daemon-down chain;
  invariant: no terminal-transition storage write executes outside
  the managed executor. R19-F2: the finalizer becomes a
  revoke-and-await settlement barrier
  (`shutdown(wait=True, cancel_futures=True)`) ordered before
  `runner.database.close()` and the pid-claim release; boundedness
  argued from single-statement workers, the pool client timeout,
  and the documented CLI force-kill backstop, with the wedge case
  failing closed through server-side transaction abort plus
  `claim_pid_file`'s dead-pid takeover
  (`runner_pid_file.py:99-138`); the pool-close argument restated
  on psycopg_pool semantics. 1.4.19 rewritten (queued and started
  complete-run and capture offloads under deadline-mid-drain, plus
  the old-daemon/new-daemon interleaving); new 1.4.20 pins the
  choke point; `storage/executor.py` and
  `tests/storage/test_database_executor.py` join the targets and V2
  list.

**Round 21** `kind: verification`

- reviewer_run: 98c03e3f-b777-4e13-83fb-0832b129082b
- verdict: needs_review (adversary review round 20; 4 blocking
  findings; artifact unmodified by reviewer; validation passed)
- findings: R20-F1 (blocking, bad-sequencing, §1.4/1.4.19): the
  proposed synchronous `shutdown(wait=True, cancel_futures=True)`
  call blocks the event-loop thread while `DatabaseExecutor.run`
  resolves through loop-owned futures
  (`storage/executor.py:60-70`), so shield-owner settlement —
  re-read, delivery, in-flight bookkeeping — cannot run before the
  finalizer proceeds to `runner.database.close()`, contradicting
  the settled-before-close guarantees. R20-F2 (blocking,
  traceability, §1.4/1.4.20): "coordinator holds the executor
  handle from construction" had no implementable wiring —
  `SessionCoordinator.__init__` accepts no executor
  (`hooks/session_coordinator.py:88-132`) and the factory, hook
  manager, and app-lifecycle chain supplies none, with none of
  those files in Targets — and the cancellation rationale was
  type-inaccurate: `Future.result()` raises
  `concurrent.futures.CancelledError`, an `Exception` subclass, so
  "invisible to except Exception" was wrong. R20-F3 (blocking,
  unhandled-edge, §1.4/1.4.19): the fail-closed argument did not
  hold on the implemented path — the post-`PoolTimeout`
  `pool.check()` is unbounded (`storage/hub/postgres.py:262-289`),
  terminal statements and COMMIT carry no server-side deadline,
  the SIGKILL backstop covers only the direct-PID stop path
  (`cli/utils_shutdown.py:38-197`), and `claim_pid_file` is an
  OS-level fence only (`runner_pid_file.py:99-138`): psycopg's
  transaction context sends COMMIT on clean exit
  (`storage/hub/postgres.py:349-373`) and PostgreSQL can complete
  a COMMIT whose client died after sending it, so a replacement
  daemon could sweep before a predecessor's late commit became
  visible, stranding a durable subscriber. R20-F4 (blocking,
  bad-sequencing, §3.1/3.1.5): the batch loop set the single
  global `merge_worker_completed=true` on the first wake while
  `current_batch_run_ids` remained non-empty, and the
  inspection, no-progress, and spawn gates keyed on that boolean
  alone, authorizing merge-state inspection, progression, or a
  replacement spawn while a batch worker was still mutating state.
- resolution_notes: all four verified against code — the
  `run_in_executor` loop-future coupling, the executor-less
  coordinator construction chain (zero `db_executor` references
  under `src/gobby/hooks`), the unbounded check-and-retry path,
  the stop-path backstop gaps, and the yaml gate expressions all
  held as cited; the CancelledError hierarchy was confirmed
  empirically on CPython 3.14.3 (`Exception` subclass, and a
  post-shutdown submit raises `RuntimeError`). R20-F1: the
  finalizer becomes drain-then-barrier — it re-runs the idempotent
  `drain_shielded_terminal_deliveries` with the loop live and the
  database open so every owned scope settles fully, then runs the
  executor barrier off-loop via `asyncio.to_thread`, so revocation
  can only ever catch coordinator sync submits and residual
  non-terminal work; 1.4.19 rewritten accordingly, including a
  loop-responsiveness probe. R20-F2: the coordinator joins the
  choke point through a module-level synchronous seam function
  (inline default, production supply points it at the executor's
  sync `submit`), leaving the construction chain untouched, and
  the failure taxonomy is named and tested exactly —
  `concurrent.futures.CancelledError` caught by name,
  `RuntimeError` on post-shutdown submit. R20-F3: new 1.4.21 —
  terminal transactions issue `SET LOCAL` statement and lock
  bounds plus a shared transaction-scoped advisory fence lock, the
  hub pool gains connect-timeout and keepalive kwargs bounding the
  validation-and-retry path, and a booting daemon acquires the
  exclusive fence counterpart between the pid claim and its first
  recovery operation, making recovery wait out and then observe
  any indeterminate predecessor COMMIT; the wedge prose now states
  the abort-vs-complete indeterminacy honestly.
  `storage/hub/postgres.py` and the pool-config and storage-agents
  tests join Targets and V2. R20-F4: batch completion is never
  derived from `merge_worker_completed` alone — the three
  inspection rules, the no-progress derivation, and the
  `spawn_agent` block gate all carry the outstanding-batch
  condition, the capture rule appends rather than overwrites,
  failed-entry re-dispatch routes through `dispatch_batch`, and
  3.1.5 pins the between-wakes state: no inspection recorded, no
  no-progress signal, spawn blocked until the list empties.

**Round 22** `kind: verification`

- reviewer_run: 2423f409-ca10-4f53-93dc-d516769b5035
- verdict: needs_review (adversary review round 21; 7 blocking
  findings; artifact unmodified by reviewer; validation passed)
- findings: R21-F1 (blocking, bad-sequencing, §1.4/1.4.18-1.4.19):
  admission closed only inside `_run_async_shutdown_cleanup`, but
  the overall `asyncio.timeout_at` wraps that call
  (`runner_lifecycle_shutdown.py:687-780`), so deadline expiry
  before cleanup starts left the finalizer draining with admission
  open and producers unquiesced — the closed-set termination
  argument failed for one of the three required exit shapes.
  R21-F2 (blocking, unhandled-edge, §1.4/1.4.19): the finalizer's
  drain and `asyncio.to_thread` barrier awaits carried no
  cancellation protection — `run_daemon` awaits
  `shutdown_daemon_services` directly (`runner_lifecycle.py:258`)
  and embedded/test callers can cancel it, letting
  `runner.database.close()` and pid release race live delivery
  work or be skipped entirely. R21-F3 (blocking, unhandled-edge,
  §1.4/1.4.19-1.4.21): drain termination was not bounded by the
  terminal-transaction bounds — acknowledged delivery also awaits
  the ISM dedup read and insert, the subscriber-row removal, and
  the per-session live-nudge locks and callbacks, all outside the
  four terminal transactions' `SET LOCAL` scope, so a stalled
  delivery statement or live callback could hold the in-flight set
  non-empty forever. R21-F4 (blocking, traceability,
  §1.4/1.4.20): the seam supply requires editing
  `runner_init/orchestration.py`, absent from 1.4's Targets and
  explicitly consumer-sweep-only in 1.3 — the leaf was not
  self-contained as written. R21-F5 (blocking, unhandled-edge,
  §1.4/1.4.21): `cleanup_stale_pending_runs` writes
  `status = 'error'` in a direct bulk transaction
  (`storage/agents/_cleanup.py:119-161`) bypassing all four
  fenced lifecycle methods — no fence lock, no bounds — reopening
  the predecessor late-COMMIT race for that writer. R21-F6
  (blocking, unhandled-edge, §1.4/1.4.21): `connect_timeout` and
  keepalive kwargs do not bound the post-`PoolTimeout`
  `pool.check()` — `check_connection` runs `conn.execute("")` on
  each established connection with no deadline
  (`psycopg_pool/pool.py:513-566`), and `connect_timeout` covers
  establishment only. R21-F7 (blocking, unhandled-edge,
  §1.4/1.4.21): the exclusive boot fence had a per-attempt
  `lock_timeout` but no overall budget or terminal behavior — a
  wedged predecessor backend holding the shared lock would retry
  startup forever.
- resolution_notes: all seven verified against code — the overall
  timeout wrapping both the graceful sequence and async cleanup,
  the direct un-armored await chain, the synchronous
  loop-thread `_send_ism` statements (`events/wake.py:498-529`)
  and per-session lock plus callbacks (`events/wake.py:144-156`),
  the missing Target, the bulk stale-pending UPDATE, the
  psycopg_pool `check()` source, and the unspecified retry policy
  all held as cited. R21-F1: admission close is now idempotent and
  two-sited — async cleanup and the finalizer's step zero — with
  the loop-thread synchronous check-then-enter argument making the
  close race-free; 1.4.19 pins the producer-after-settlement
  shape. R21-F2: the finalizer runs the full settlement — close,
  drain, barrier, database close, pid cleanup — as one owned task
  awaited through a shield-and-rewait loop that absorbs repeated
  cancellation and re-raises only after settlement; 1.4.19 pins
  cancellation at the drain await, at the barrier await, and a
  second cancellation. R21-F3: a shared bounded-transaction helper
  on the hub carries the `SET LOCAL` bounds to the delivery-path
  statements (ISM dedup read, ISM insert, subscriber-row removal,
  bounded terminal re-read) and `WakeDispatcher.wake` bounds the
  live-nudge phase with a client-side deadline after durable ISM
  persistence — a bound overrun lands in `_send_ism`'s existing
  broad handler and settles the scope in the retained-row state,
  so the drain still reaches stable empty;
  `inter_session_messages.py`, `pipeline_subscribers.py`, and
  `wake.py` join Targets with serialization notes. R21-F4:
  `runner_init/orchestration.py` joins 1.4's Targets, the
  parenthetical marks it edited here, and 1.4.20 pins the
  production supply of both seams at the daemon-init wiring site.
  R21-F5: the stale-pending bulk transaction adopts the same
  bounds-plus-shared-fence first statements — five terminal-status
  transactions total — and the recorded source sweep confirms no
  other direct `status` writer exists (`cleanup_stale_runs`
  terminalizes through the fenced `timeout`; the runtime,
  termination, messaging, and spawn-failure UPDATE sites never
  write `status`); 1.4.21 pins the stale-pending late-COMMIT
  interleaving. R21-F6: the false kwargs claim is removed — the
  acquisition-retry path sheds the synchronous `pool.check()` for
  a second bounded acquire attempt under the existing 5-second
  client timeout, kwargs keep concrete establishment and dead-peer
  values, and 1.4.21 pins the never-returning validation-query
  shape. R21-F7: the exclusive fence carries a finite acquisition
  budget — five attempts, five-second `SET lock_timeout` each,
  one-second backoff — and exhaustion fails closed: startup aborts
  with a `pg_locks`/`pg_stat_activity` diagnostic, no recovery
  runs, and every durable subscriber row is preserved; 1.4.21 pins
  the perpetual-shared-holder shape.

**Round 23** `kind: verification`

- reviewer_run: 74221626-559d-435c-8f97-52032571652b
- verdict: needs_review (adversary review round 22; 4 blocking
  findings; artifact unmodified by reviewer)
- findings: R22-F1 (blocking, unhandled-edge,
  §1.4/1.4.19–1.4.21): the round-21 bounded-statement list did not
  cover `WakeDispatcher.wake`'s real chain — the pre-ISM session
  lookup is a synchronous unbounded point read
  (`events/wake.py:111-142`,
  `storage/sessions/_identity_crud.py:56-61`), the SDK nudge branch
  performs two more synchronous reads that no asyncio deadline can
  interrupt (`events/wake.py:473-496`), the dedup path swallows any
  exception from the bounded read and falls back to an unbounded
  `list_messages` (`events/wake.py:531-583`), and unconditionally
  bounding `create_message` would leak `SET LOCAL` into the mailbox
  sender's ambient transaction (`sessions/mailbox.py:157-174`).
  R22-F2 (blocking, bad-sequencing, §1.4/1.4.21): the fence gate
  was sited in the recovery module, but subsystem initialization is
  a background task started only after HTTP is serving with a
  log-only failure callback (`runner_lifecycle.py:111-232`,
  `runner_lifecycle_subsystems.py:789-854`), so fence exhaustion
  would occur with traffic already accepted, contradicting the
  fail-closed claims and letting this daemon's own writers race the
  fence. R22-F3 (blocking, unhandled-edge, §1.4/1.4.21): the
  post-exhaustion holder diagnostic is itself a live query with no
  specified bound — generic hub fetches carry no statement deadline
  (`storage/hub/postgres.py:401-415`) — so a stalled diagnostic
  defeats the claimed finite abort. R22-F4 (blocking,
  unhandled-edge, §1.4/1.4.18–1.4.20): the admission flag was
  specified close-only; `run_daemon` supports embedded callers and
  returns without terminating the interpreter
  (`runner_lifecycle.py:111-143`), so a second daemon lifecycle in
  the same interpreter would inherit a closed boundary and never
  perform transition work again.
- resolution_notes: all four verified against code — the sync
  session read, the SDK branch's catch-all double read, the dedup
  fallback swallow, the mailbox ambient transaction, the
  serve-then-init ordering with its log-only done callback, the
  deadline-free generic fetch, and the embedded-caller lifecycle
  all held as cited. R22-F1: the bounds move to call-site wrappers
  on the shared helper — `wake` bounds its initial session lookup,
  `_send_ism` runs dedup read plus message insert in one
  helper-owned transaction the manager statements join ambiently
  (the message manager and its mailbox caller stay byte-for-byte
  unchanged), the subscriber-row removal is wrapped at its cleanup
  call site on the executing thread, SDK resolution reuses the
  session `wake` already loaded and bounds any residual lookup
  behind its existing best-effort catch, and
  `_notification_exists`'s handlers re-raise the bound-expiry
  class so durable-phase expiries reach `_send_ism`'s handler
  instead of the fallback, which itself joins the same bounds;
  `inter_session_messages.py` and `pipeline_subscribers.py` leave
  Targets (no longer edited), `agent_cleanup.py` and
  `runner_lifecycle.py` join 1.4.21's files, and 1.4.21 pins the
  four never-returning-read shapes plus the wake-lock release.
  R22-F2: the fence becomes a boot gate awaited in `run_daemon`
  between the pid claim and Uvicorn server creation — before any
  request handler, subsystem task, or own shared-mode writer
  exists — with the acquisition helper defined beside the recovery
  sweeps; the parenthetical marks `runner_lifecycle.py` edited,
  and 1.4.21 pins the perpetual-holder abort as
  server-never-started, database-closed, pid-released,
  rows-preserved. R22-F3: the holder diagnostic runs under the
  shared helper's bounds as one more entry in the startup budget
  (worst case near thirty-five seconds), and on diagnostic timeout
  or failure the abort proceeds with a fallback message carrying
  the fence key and the diagnostic error; 1.4.21 pins the
  diagnostic-never-returns shape. R22-F4: the admission flag gains
  a full lifecycle — the seam-supply init block asserts the prior
  in-flight set is empty and reopens admission before any producer
  or HTTP service runs; 1.4.20 pins the sequential
  embedded-lifecycle shape (daemon A settles, daemon B in the same
  interpreter completes transition plus acknowledged delivery).

**Round 24** `kind: verification`

- reviewer_run: 35e6d265-b731-4568-8510-efccd31114db
- verdict: needs_review (adversary review round 23; 1 blocking
  finding; artifact unmodified by reviewer)
- findings: R23-F1 (blocking, unhandled-edge, §1.4/1.4.21): the
  claimed near-thirty-five-second startup worst case excluded
  connection-pool acquisition, which precedes every helper entry's
  first statement — `_pool_connection` performs two acquisition
  attempts after `PoolTimeout` (`storage/hub/postgres.py:262-289`),
  each under the configured five-second acquire timeout
  (`config/postgres_pool.py:10-16`) — so five fence attempts, four
  backoffs, and the diagnostic could approach ninety-four seconds
  under pool starvation, and the 1.4.21 shapes did not prove the
  stated end-to-end bound.
- resolution_notes: verified against code — the two-attempt acquire
  path and the `acquire_timeout_seconds: float = 5.0` default both
  held as cited, putting up to ten seconds of acquisition ahead of
  each attempt's `SET lock_timeout` wait. Fix: the additive
  arithmetic is replaced by a single end-to-end gate deadline —
  sixty seconds on a monotonic clock, measured at gate entry —
  covering pool acquisition, all lock attempts, backoffs, and the
  holder diagnostic, with per-step admission control: a step
  starts only if the remaining budget covers its fifteen-second
  worst case (two bounded acquires plus the five-second statement
  wait), so every admitted step completes inside the deadline and
  no step starts past it; on budget exhaustion the diagnostic is
  skipped and the fallback abort message records the skip. 1.4.21
  updates both abort shapes to the declared deadline and pins the
  deterministic pool-starvation-plus-perpetual-holder case: with
  every helper entry starved to its bounded two-attempt worst case
  while a perpetual holder keeps the shared fence, the
  server-never-started, database-closed, pid-released,
  rows-preserved abort lands within the declared deadline.

**Round 25** `kind: verification`

- reviewer_run: e0ff92ec-1f92-430d-aba5-e5ab8e911f37
- verdict: needs_review (adversary review round 24; 2 blocking
  findings; artifact unmodified by reviewer)
- findings: R24-F1 (blocking, unhandled-edge, §1.4/1.4.21): the
  fifteen-second per-step figure was not a true worst case —
  helper entry runs `self.open()`, `_pool_connection()`, and
  `conn.transaction()` (`storage/hub/postgres.py:349-373`), the
  proposed `SET LOCAL statement_timeout` cannot bound the SET
  statement that installs it, and transaction exit performs
  COMMIT or ROLLBACK, which the plan itself concedes carries no
  statement bound — so an admitted helper call could outlive the
  monotonic deadline; admission control bounds when steps start,
  not how long they run. R24-F2 (blocking, traceability, §1.4):
  the superseded thirty-five-second figure survived in normative
  text as "previously claimed thirty-five"; superseded arithmetic
  belongs only in V1 history.
- resolution_notes: verified against code — `_transaction_context`
  confirmed the setup/teardown path as cited, and the COMMIT
  concession already stood in §1.4. Fix for R24-F1: the deadline
  is now enforced structurally rather than derived from statement
  arithmetic — the gate body runs on a dedicated
  daemon-flagged worker thread whose loop-side completion future
  the gate coroutine awaits under `asyncio.wait_for` with the
  remaining deadline (never a pool executor or
  `asyncio.to_thread`, whose non-daemon threads are joined at
  interpreter shutdown and would let a wedged helper block
  process exit); admission control is demoted to scheduling
  policy over the costs client and server timeouts do bound
  (the fifteen-second admission cost), and on watchdog expiry the
  boot side stops waiting, aborts with a fallback message
  recording the expiry, and the abandoned worker's severed
  in-flight fenced transaction resolves server-side by rollback,
  with a race-window commit standing as atomic completed work.
  1.4.21 pins four new deterministic never-returning shapes:
  helper setup hang, first `SET LOCAL` that never returns, COMMIT
  that never returns, ROLLBACK that never returns — each landing
  the server-never-started, database-closed, pid-released,
  rows-preserved abort within the declared deadline. Fix for
  R24-F2: the "previously claimed thirty-five" comparison and the
  round-23 falsification narrative are removed from §1.4; the
  ninety-four-second figure remains only as current motivation
  for why statement arithmetic cannot deadline the gate, and
  superseded figures stay confined to V1 history.

**Round 26** `kind: verification`

- reviewer_run: f9fa9ee5-eb60-4672-ba6a-31334a5b4c5d
- verdict: needs_review (adversary review round 25; 2 blocking
  findings; artifact unmodified by reviewer)
- findings: R25-F1 (blocking, unhandled-edge, §1.4/1.4.21): the
  watchdog proof depended on interpreter death while the plan
  preserves `run_daemon`'s embedded contract — it is a directly
  awaitable API that can return without terminating the
  interpreter (`runner_lifecycle.py:111-115`), its fatal handler
  raises `SystemExit` only from the `except Exception` arm
  (`runner_lifecycle.py:283-288`) which an embedded host may
  catch, and caller cancellation bypasses that handler — so the
  abandoned worker thread could survive the abort, and because
  `PostgresHubDatabase.close` delegates to `pool.close`
  (`storage/hub/postgres.py:483-491`), which leaves checked-out
  connections open until returned, the worker could keep using
  its connection after database close and pid release,
  contradicting the deadline and safe-abort guarantees. R25-F2
  (blocking, unhandled-edge, §1.4/1.4.21): the revised watchdog
  text claimed every severed in-flight fenced transaction
  resolves by rollback, contradicting the plan's own COMMIT
  model — after COMMIT is sent, PostgreSQL may complete it after
  client death and the client-visible outcome is indeterminate —
  and 1.4.21 applied universal rollback even to the
  COMMIT-that-never-returns shape.
- resolution_notes: verified against code — the `run_daemon`
  docstring names embedded/test callers, the fatal handler is
  `except Exception` → pid cleanup → `sys.exit(1)`, and
  `close()` delegates to `pool.close()` as cited. Fix for
  R25-F1: gate execution is now independently killable and
  settled before database close and pid release — the gate body
  (establishment, attempts, backoffs, diagnostic) runs in a
  terminable child process, a minimal `-m`-launched entry module
  (`runner_gate.py`, psycopg and stdlib only) fed DSN, fence
  key, and remaining budget as JSON on stdin, never argv or
  environment; the child holds one dedicated direct connection
  and never touches the hub pool, so no checked-out pooled
  connection can exist for the gate and pool close never waits
  on it; the helper awaits the child under `asyncio.wait_for`
  inside a `try/finally` whose finally kills (SIGKILL) and reaps
  the child before any unwind continues — watchdog expiry, gate
  failure, and caller cancellation alike. Admission costs are
  re-derived off-pool: ten-second establishment under
  `connect_timeout`, five-second lock wait per attempt,
  five-second diagnostic; the pool-starvation ninety-four-second
  narrative and fifteen-second per-step figure leave normative
  text with the pool's departure from the gate. 1.4.21 gains the
  two required same-interpreter shapes — embedded cancellation
  of the gate await (child killed and reaped before the unwind;
  second lifecycle acquires cleanly) and an embedded host
  catching the fatal `SystemExit` (child settled before
  close/release; second lifecycle starts safely) — and the gate
  files list plus the 1.4 Targets gain the child module. Fix for
  R25-F2: commit/rollback indeterminacy is restored — severance
  before COMMIT rolls back, while an accepted COMMIT may
  complete after client death, committing no row change and
  releasing the advisory lock at commit — and both outcomes are
  safe because no gate statement writes a row (two `SET LOCAL`s
  plus the exclusive advisory-lock acquisition per attempt, and
  a read-only diagnostic join); the four never-returning 1.4.21
  shapes are rewritten as child-side stalls asserting safety
  under both outcomes rather than universal rollback.

**Round 27** `kind: verification`

- reviewer_run: a14d8e06-e2a8-4b0e-b0ed-213ede84977c
- verdict: needs_review (adversary review round 26; 2 blocking
  findings; artifact unmodified by reviewer)
- findings: R26-F1 (blocking, unhandled-edge, §1.4/1.4.21): the
  gate child was settled before its await unwound, but the plan
  still assumed cleanup the pre-server lifecycle never performs —
  normative text said watchdog failure reaches database close and
  pid release and that caller cancellation permits a second
  same-interpreter lifecycle, while in code the only pid-release
  helper is `cleanup_owned_pid_file`
  (`runner_lifecycle.py:137-143`), the fatal arm catches only
  `Exception` and raises `SystemExit` without closing
  `runner.database`, cancellation bypasses that arm with only
  `clear_app_context()` in the outer finally
  (`runner_lifecycle.py:283-288`), and the sole close-then-release
  sequence lives in `shutdown_daemon_services`' finalizer, reached
  only after Uvicorn setup (`runner_lifecycle.py:170-258`,
  `runner_lifecycle_shutdown.py:687-780`) — so gate-await
  cancellation leaked the pid claim and database, and gate failure
  plus caught `SystemExit` released the pid with the database
  open. R26-F2 (blocking, unhandled-edge, §1.4/1.4.19-1.4.21): the
  terminable child repaired only the boot gate while the finalizer
  drain's terminal-transition and delivery transactions kept the
  same client-unbounded shapes the plan itself concedes — helper
  setup, the first `SET LOCAL`, and COMMIT/ROLLBACK teardown
  (`storage/hub/postgres.py:349-373`) — with the wedged worker
  awaited through `DatabaseExecutor.run`
  (`storage/executor.py:60-70`), so a stall in any drain-dependent
  transaction could keep the cancellation-armored drain alive
  forever in an embedded host, defeating 1.4.19's
  settlement-before-close/pid-release guarantee.
- resolution_notes: verified against code — `run_daemon`'s nested
  pid helper, `except Exception`-only fatal arm, and
  post-Uvicorn-only `shutdown_daemon_services` call held as cited,
  as did the finalizer's `finally`-block close-then-release
  ordering (`runner_lifecycle_shutdown.py:762-773`), the
  transaction entry's unbounded `BEGIN`/teardown, and the
  executor's awaited worker. Fix for R26-F1: the gate await site
  in `run_daemon` now owns early-startup cleanup for gate failure
  and cancellation alike, in pinned order — child-reap (already
  done in the helper's finally), then `runner.database` close with
  close-failure logging that never blocks the next step, then pid
  release, then the preserved outcome: gate failure logs the fatal
  diagnostic and raises `SystemExit(1)` directly (never through
  the `except Exception` arm, so no double cleanup), cancellation
  re-raises `CancelledError`; 1.4.21's two embedded shapes now pin
  the child-reap → database-close → pid-release order, the
  close-failure handling, and a successful same-interpreter second
  pid claim in both shapes. Fix for R26-F2: the finalizer re-drain
  and executor barrier run under one declared client-side
  finalizer deadline — ten seconds, monotonic, measured at
  finalizer entry — whose expiry logs each abandoned scope by run
  id, retains its durable rows (the existing retention branch),
  and proceeds to close, pid release, and the preserved outcome;
  abandonment is made safe against
  predecessor-writes-after-ownership-transfer by a new severance
  sweep in the gate child, run while the exclusive fence lock is
  held: hub-pool connections gain a lifecycle-scoped
  `application_name` marker (`gobby-hub-` plus a per-open nonce),
  and the sweep terminates every other-lifecycle hub-marker
  backend in the current database under the transaction's
  `SET LOCAL` bounds as one more admitted five-second entry —
  fence-holding predecessor transactions resolve before the
  exclusive grant (the pinned interleaving), every other
  predecessor backend dies before the lock releases, and the
  predecessor's closed pool can mint no replacement, while
  terminated backends also unwedge abandoned executor workers so
  leaked threads self-heal. 1.4.19 gains the never-returning
  first-`SET LOCAL`/COMMIT/ROLLBACK shapes for both
  terminal-transition and delivery transactions plus the
  deadline-expiry retention outcome; 1.4.21 gains the sweep
  assertions (exclusive-lock ordering, marker filtering,
  fail-closed sweep expiry, killed pre-fence worker never
  commits).

**Round 28** `kind: verification`

- reviewer_run: 5047142a-7033-487b-807d-4866a6b7dcf9
- verdict: needs_review (adversary review round 27; 4 blocking
  findings; artifact unmodified by reviewer)
- findings: R27-F1 (bad-sequencing, §1.4/1.4.19–1.4.21): the
  embedded-host recovery loop could not reach the severance gate —
  1.4.20 required daemon initialization to assert the prior
  lifecycle's in-flight set empty before reopening admission, but
  `GobbyRunner.__init__` runs `init_orchestration` during
  construction (`runner.py:183-195`), before `run_daemon` ever
  awaits the gate, so a finalizer-deadline-abandoned scope left
  the set non-empty, tripped the successor's constructor
  assertion, and the gate child that would sever the wedged
  backend never started. R27-F2 (unhandled-edge, severance sweep):
  the sweep treated termination as immediate, but
  `pg_terminate_backend` with the zero-timeout default returns
  true on successful signal delivery, not on process exit
  (PostgreSQL 18 system-administration functions), so a signaled
  pre-fence backend could survive the exclusive-lock release and
  defeat the no-write-after-ownership-transfer proof. R27-F3
  (traceability): superseded unconditional settlement claims —
  "re-drains every owned scope to settlement", "the in-flight set
  empties", and 1.4.19's settle-fully branch — coexisted with the
  round-26 ten-second expiry branch as mutually exclusive
  acceptance contracts. R27-F4 (weak-testability): the expiry
  branch promised every abandoned scope's durable rows retained,
  but the delivery chain removes acknowledged rows in a later
  transaction whose COMMIT teardown is client-unbounded
  (`storage/hub/postgres.py:349-373`), so abandonment landing
  during an indeterminate removal COMMIT can leave the row
  deleted — universal retention unimplementable.
- resolution_notes: all four verified against code and artifact
  before edits — the `GobbyRunner.__init__` construction order,
  the zero-timeout signal-only semantics of
  `pg_terminate_backend`, both unconditional-settlement narrative
  sites plus 1.4.19's branch, and the removal transaction's
  unbounded COMMIT shape all held as cited. R27-F1: the finalizer
  expiry branch now detaches each abandoned owner — an idempotent
  discard from the tracked in-flight set that the abandoned task's
  own eventual finalizer re-runs as a no-op — so both predecessor
  exits leave the set empty and 1.4.20's init-time assertion holds
  unconditionally; abandoned owners hold only
  predecessor-lifecycle handles captured at scope entry, so a
  post-severance unwind exercises predecessor objects alone and
  never touches successor seams; 1.4.20 gains the required
  same-interpreter recovery-loop test (abandoned scope in daemon
  A, daemon B initializes with the assertion passing, claims the
  pid, gate severs A's wedged backend, A's worker unwinds, B
  completes acknowledged delivery). R27-F2: the sweep is now a
  terminate-and-verify loop — positive-timeout
  `pg_terminate_backend` plus a marker-predicate re-query, with
  completion defined only by zero matching backends and false
  returns resolved through the re-query — and residual matching
  backends, bound expiry, budget exhaustion, or a failed sweep
  query fail the gate closed; 1.4.21 gains the
  delayed-termination assertion that the exclusive lock is never
  released with a signaled-but-alive backend present. R27-F3:
  every settlement/stable-empty assertion is qualified by the
  ten-second finalizer budget — the narrative and 1.4.19 now
  define disjoint within-budget (settle fully, set empty by
  settlement, no terminal commit after close) and expiry (logged,
  detached, abandoned; set empty by detachment) branches, and the
  admission-reopen narrative cites both exits. R27-F4: retention
  split by phase — a scope abandoned before acknowledged delivery
  retains its subscriber rows, a scope abandoned during its
  post-acknowledgement row-removal COMMIT passes with rows present
  or absent, both safe under the
  delete-only-after-acknowledged-delivery invariant; 1.4.19 pins
  the invariant and both indeterminate outcomes, and the fourth
  abandonment-safety ground now reads accordingly.

**Round 29** `kind: verification`

- reviewer_run: 456131ba-e54e-4bea-a4b5-756593154d5b
- verdict: needs_review (adversary review round 28; 3 blocking
  findings; artifact unmodified by reviewer)
- findings: R28-F1 (unhandled-edge, §1.4.19–1.4.21): the
  ten-second finalizer watchdog could not govern the
  delivery-side client-unbounded shapes —
  `CompletionEventRegistry.notify` awaits the wake callback on
  the registry-owning loop (`events/completion_registry.py:83-124`)
  and `WakeDispatcher.wake` ran its session lookup and
  `_send_ism` synchronously before its next await
  (`events/wake.py:111-142`), so a delivery transaction stalled
  in `BEGIN`, the first `SET LOCAL`, or COMMIT/ROLLBACK
  (`storage/hub/postgres.py:349-373`) wedged the event-loop
  thread itself, the asyncio deadline could never fire, and the
  expiry detach, database close, and pid release were
  unreachable — the acceptance case claiming a stalled delivery
  transaction is abandoned was unimplementable on the real call
  chain. R28-F2 (bad-sequencing, finalizer barrier): the expiry
  branch had no implementable barrier ordering —
  `asyncio.to_thread(db_executor.shutdown, wait=True,
  cancel_futures=True)` under the budget either defeats the
  deadline (awaited) or leaves database close running before the
  barrier completes (timed out), the exact
  wait=True-in-to_thread shape the code warns against
  (`runner_lifecycle_shutdown.py:542-552`), and the executor's
  non-daemon workers (`storage/executor.py:42-58`, `:105-111`)
  can pin a standalone daemon's interpreter exit with no
  successor running. R28-F3 (weak-testability, §1.3/1.4.19): the
  repaired removal-COMMIT safety argument covered only
  `ism_persisted` acknowledgements — the classifier also treats
  `session_not_found` as delivered, but `wake` returns that code
  before `_send_ism` runs (`events/wake.py:111-128`), so no
  durable notification exists and the "stored acknowledged
  notification" justification was false for that branch.
- resolution_notes: all three verified against code before
  edits — the notify→wake loop-thread call chain, the wait=False
  comment and blocking executor shutdown, and the
  session_not_found early return all held as cited. R28-F1:
  every delivery-path database transaction under the finalizer
  proof — wake session lookup, `_send_ism` dedup-and-insert,
  residual SDK lookups, subscriber-row removal — now executes
  through the managed executor offload on a worker thread,
  submitted against the scope's entry-captured executor handle
  and joined to the 1.4.20 choke point and 1.4.21 bounds; a
  client-unbounded delivery stall wedges only its worker, and
  1.4.19 pins real-chain stall tests for first
  SET LOCAL/COMMIT/ROLLBACK on both the durable-wake and
  row-removal transactions with a loop probe, watchdog expiry,
  and pinned-branch outcome before detachment. R28-F2: the
  barrier is branch-disjoint — within budget the wait=True join
  runs on a dedicated Gobby-owned daemon barrier thread signaled
  back via `call_soon_threadsafe` and awaited under the
  remaining budget; on expiry the finalizer synchronously runs
  `shutdown(wait=False, cancel_futures=True)` (idempotent
  against a started join via the `_shutdown` guard), never
  awaits the join, and accounts started submissions through
  tracked ownership; the ordering, pool-close, and
  no-commit-after-release claims are branch-qualified, and
  process exit is accounted per shape — pid release precedes any
  interpreter-exit join, the CLI force-kill covers supervised
  teardown, `main()` gains an expiry-branch-only `os._exit`
  backstop, and the embedded host returns with leaked workers
  documented until severance — with subprocess coverage for the
  standalone shape and embedded cancellation/SystemExit pinned
  in 1.4.19. R28-F3: the removal-COMMIT proof and 1.4.19
  acceptance split by acknowledgement reason — `ism_persisted`
  rests on the stored notification and ISM dedup;
  `session_not_found` rests on permanent session absence, with a
  surviving row reclassified and removed by the next boot's
  sweep and no claim that a notification was stored — and the
  fourth abandonment-safety ground reads accordingly.

**Round 30** `kind: verification`

- reviewer_run: 79efc2c5-4ad3-48e8-a9cd-098d8995cd39
- verdict: needs_review (adversary review round 29; 2 blocking
findings; artifact unmodified by reviewer)
- findings: R29-F1 (bad-sequencing, §1.4/1.4.19): the
  expiry-branch idempotency proof was false against
  `storage/executor.py:105-111` — `DatabaseExecutor.shutdown`
  writes its `_shutdown` flag under the lock but calls the
  underlying `ThreadPoolExecutor.shutdown` only after releasing
  it, so a barrier thread descheduled between the flag write and
  the underlying call would make the expiry-branch `wait=False`
  call a guard-hit no-op with nothing revoked, and the finalizer
  would close the database and release the pid with queued
  futures still startable. R29-F2 (bad-sequencing,
  §1.4/1.4.20–1.4.21): the process-global seam supply and
  admission assertion/reopen were sited at construction —
  `GobbyRunner.__init__` runs `init_orchestration`
  (`runner.py:183-195`) while embedded/test callers acquire or
  lose the pid claim only inside `run_daemon`
  (`runner_lifecycle.py:111-170`) — so a contending runner B
  could fail the empty-set assertion against a serving daemon A,
  or overwrite A's module seams and then lose the ownership
  check, breaking predecessor-handle isolation and leaving the
  round-27 same-interpreter repair incomplete.
- resolution_notes: both findings verified against
  `storage/executor.py:105-111` (flag written under the lock,
  underlying shutdown outside it), `runner.py:183-195`
  (`init_orchestration` runs in `__init__`), and
  `runner_lifecycle.py:111-170` (pid claim acquired inside
  `run_daemon`). Fix for R29-F1: the shutdown protocol separates
  close-and-revoke from joining in `DatabaseExecutor` itself —
  `shutdown` becomes the non-blocking close-and-revoke whose
  flag write and underlying
  `shutdown(wait=False, cancel_futures=True)` execute together
  inside the executor lock, so an observed guard implies
  completed revocation by lock atomicity, and a new guard-free
  `join` method performs only the blocking worker join; the
  finalizer runs close-and-revoke synchronously from the loop on
  both branches ahead of `runner.database.close()` — at expiry
  in the mid-drain shape, ahead of the join otherwise — and
  sends only the join to the dedicated daemon barrier thread on
  the within-budget branch; the ordering guarantee is now
  branch-invariant for revocation and branch-qualified only for
  the join; 1.4.19 gains the deterministic interleaving test
  pausing the first `shutdown` caller between its flag write and
  the underlying pool shutdown while the expiry branch runs
  close-and-revoke concurrently, plus `storage/executor.py` and
  `tests/storage/test_database_executor.py` in its file and test
  lists. Fix for R29-F2: every process-global mutation — module
  seam supply, empty-set assertion, admission reopen — moves to
  a post-claim lifecycle-activation step `run_daemon` invokes
  only after the pid claim is held and the boot gate has
  completed, still before the Uvicorn server object exists and
  before any producer runs; `GobbyRunner` construction supplies
  only instance-local wiring (the `run_db` defaults and the
  dispatcher's executor handle) and writes no module seam and no
  admission state; 1.4.20 pins the activation siting, two
  same-interpreter contention tests (daemon A serving with its
  in-flight set nonempty and again empty, daemon B constructed
  and run to contention losing cleanly with A's admission state
  and both seams unchanged), and the recovery-loop test
  reordered to claim → gate → activation → assertion; the §1.4
  inventory re-scopes `runner_init/orchestration.py` to the
  instance-local dispatcher-executor wiring and adds the
  activation step to `runner_lifecycle.py`'s edits;
  `src/gobby/runner_lifecycle.py` joins 1.4.20's file list.
  Validation passed after edits.

**Round 31** `kind: verification`

- reviewer_run: 74d3e422-2fd7-4513-bf3e-a324550666a9
- verdict: needs_review (adversary review round 30; 1 blocking
finding; artifact unmodified by reviewer)
- findings: R30-F1 (bad-sequencing, §1.4/1.4.20–1.4.21): the
  post-claim activation step did not make a contended
  same-interpreter runner mutation-free as the round-30 text
  claimed — `GobbyRunner.__init__` unconditionally runs every
  init block (`runner.py:183-195`) and those blocks already
  mutate process-global state (logging and telemetry at
  `runner_init/storage.py:70,148,192`, daemon-wide tmux helpers
  at `runner_init/orchestration.py:136` and
  `agents/tmux/__init__.py:59-67`, the global `ServiceContainer`
  and module event callbacks at `runner_init/servers.py:79,163`
  and `app_context.py:267-276`) and open database/executor
  resources (`runner_init/storage.py:106-112`), while the
  embedded entry constructs before the claim
  (`runner.py:212-218`; `runner_lifecycle.py:157-175`),
  `run_daemon` overwrites `_startup_tracker` and signal handlers
  pre-claim (`runner_lifecycle.py:147-165`;
  `runner_maintenance.py:928-981`), and its unconditional
  `finally` clears the global app context on the loser's return
  (`runner_lifecycle.py:288`) — so a contending runner B could
  overwrite or clear daemon A's app context, signal routing,
  tmux/broadcast state, and logging/telemetry, and leak B's
  database and executor, before reaching activation; the two
  contention tests pinned only admission and the two new seams.
- resolution_notes: every citation verified against source —
  `init_storage_and_config` runs `setup_file_logging`,
  `init_telemetry`, `add_span_storage_exporter`, and opens
  `init_hub_database` plus `DatabaseExecutor`;
  `init_orchestration` calls `configure_tmux`, which writes the
  tmux module globals; `init_servers` calls `set_app_context`
  and `setup_agent_event_broadcasting`; `run_gobby` constructs
  before `run_daemon`'s embedded claim; `_startup_tracker` and
  `setup_signal_handlers` precede that claim; `run_daemon`'s
  terminal `finally` runs `clear_app_context` unconditionally.
  Fix: ownership now precedes `GobbyRunner` construction in
  every supported entry shape — `main()` already claims first
  (`runner.py:297-315`); `run_gobby` gains the embedded claim
  ahead of `GobbyRunner(...)`, with a contended claim logging
  and returning before construction (nothing written, nothing
  opened) and `OSError` keeping the documented
  fail-open-unlocked semantic; `run_daemon`'s embedded claim
  block is deleted, making its `_startup_tracker` write,
  signal-handler installation, and terminal
  app-context-clearing `finally` reachable only under resolved
  ownership. The activation step keeps its post-gate siting for
  seam supply, empty-set assertion, and admission reopen. The
  narrative's "mutates nothing" claim now rests on the loser
  never constructing, not on activation ordering. 1.4.20's
  contention tests are reworked to pin the pre-construction
  boundary — construction observed never to run and daemon A's
  full global surface intact (admission state, both seams,
  app-context identity, signal routing, tmux helpers, no leaked
  second pool or executor) — plus a fail-open `OSError` case;
  the settled-predecessor test now resolves B's claim ahead of
  construction and the recovery-loop test reorders to
  claim → initialize → gate → activation. `src/gobby/runner.py`
  joins 1.4.20's file list and the §1.4 targets inventory;
  `tests/test_runner_pid_file.py` joins 1.4.20's test list; the
  §1.4 serialization note records `run_daemon`'s deleted claim
  block and `run_gobby`'s pre-construction claim. Validation
  passed after edits.

**Round 32** `kind: verification`

- reviewer_run: cffc3697-e1e8-4c19-9ff5-49d60dff5d36
- verdict: needs_review (adversary review round 31; 3 blocking
findings; artifact unmodified by reviewer)
- findings: R31-F1 (bad-sequencing, §1.4/1.4.20–1.4.21):
  deleting `run_daemon`'s embedded claim did not make ownership
  mandatory for its remaining entry paths — `GobbyRunner.run`
  and `run_daemon` both default `pid_claim=None`
  (`runner.py:197-200`; `runner_lifecycle.py:111`), and bare
  `runner.run()` / `run_daemon(runner)` callers are live across
  `tests/test_runner_lifecycle.py`,
  `tests/test_runner_pid_file.py`, and
  `tests/test_runner_shutdown.py`, so after deletion either
  call reaches the `_startup_tracker` write, signal-handler
  installation, and app-context mutation with no resolved
  ownership; 1.4.20 named only `main()` and `run_gobby` and
  supplied no non-bypassable witness. R31-F2 (unhandled-edge,
  §1.4/1.4.20): the run_gobby-owned claim had no release path
  on construction failure — `run_gobby` constructs and awaits
  with no try/finally (`runner.py:212-218`),
  `GobbyRunner.__init__` runs four fallible init blocks
  including the deterministic missing-config
  `FileNotFoundError` (`runner_init/storage.py:60-65`), on
  that branch `run_daemon` never starts so its release helper
  (`runner_lifecycle.py:138-143`) is unreachable, and
  `main()`'s idempotent `finally` (`runner.py:321-325`) covers
  only claims `main()` owns, so an embedded claim would remain
  held in the continuing interpreter. R31-F3 (traceability,
  V2/1.4.20): 1.4.20 assigns the round-30 contention and
  fail-open regressions to `tests/test_runner_pid_file.py`,
  but V2's focused protected-test inventory omitted that file.
- resolution_notes: every citation verified against source —
  both defaulted signatures and the forwarding chain, ~25 bare
  `runner.run()` / `run_daemon(runner)` call sites across the
  three cited test files, the absent try/finally around
  construction, the deterministic `FileNotFoundError`, the
  unreachable release helper, `main()`'s main-claims-only
  `finally`, and the V2 omission. Fix: resolved ownership is
  now explicit — a held `PidFileClaim` or a distinct
  fail-open-unlocked resolution produced only by the `OSError`
  branch; contention is never represented, because a contended
  entry exits before construction — and is a required,
  non-default parameter of both `GobbyRunner.run` and
  `run_daemon`, replacing the `None` defaults, so calling
  without a resolution is a `TypeError`. Every direct caller
  is swept to resolve ownership before `GobbyRunner`
  construction (a per-test claim on the isolated home's pid
  file, or the explicit fail-open resolution where the test
  targets unlocked behavior), and a signature-witness test
  pins that the ownership parameters have no default.
  `run_gobby` releases any claim it acquired, idempotently,
  when construction or startup raises, with a regression test
  in which the claim succeeds and construction raises, proving
  the lock admits no contender while held and is freed for a
  subsequent lifecycle; `run_gobby`'s pre-construction
  contended-return and `OSError` fail-open handling are
  preserved unchanged. `tests/test_runner_shutdown.py` joins
  1.4.20's test list; `tests/test_runner_pid_file.py` and
  `tests/test_runner_shutdown.py` join V2's focused list; the
  §1.4 serialization note records the required-ownership
  signatures and `run_gobby`'s failure-path release.
  Validation passed after edits.

**Round 33** `kind: verification`

- reviewer_run: c1a02457-a8c7-4dc4-834a-5f14ebb69fed
- verdict: needs_review (adversary review round 32; 1 blocking
finding; artifact unmodified by reviewer)
- findings: R32-F1 (unhandled-edge, §1.4/1.4.20): the round-31
  failure-path release freed the singleton claim on
  construction failure without rolling back partial
  construction — `GobbyRunner.__init__` runs four fallible
  init blocks with no rollback (`runner.py:183-195`), the
  first opens the hub database and `DatabaseExecutor` before
  many later fallible steps
  (`runner_init/storage.py:106-112`), and `init_servers`
  publishes the process-global app context before the Codex,
  web-chat, HTTP, and WebSocket constructors run
  (`runner_init/servers.py:79-112`) — so a mid-construction
  raise in the embedded `run_gobby` entry
  (`runner.py:212-218`) released the lock while the
  continuing interpreter still owned a partial pool, live
  executor threads, and mutated globals, letting a successor
  acquire the freed lock and construct over the leftovers;
  the only pinned regression used the pre-resource
  missing-config `FileNotFoundError`
  (`runner_init/storage.py:60-65`), which cannot detect this
  branch.
- resolution_notes: every citation verified against source —
  the rollback-free four-stage `__init__`, the early
  database-and-executor creation, `set_app_context` ahead of
  the Codex/web-chat/HTTP/WebSocket constructors, the
  `run_gobby` entry shape, and two further
  construction-installed process-globals inventoried beyond
  the finding: the span-exporter registration latched on the
  global tracer provider and holding the lifecycle's
  `SpanStorage` (`telemetry/providers.py:54-69`, torn down
  by the existing `shutdown_providers` at
  `telemetry/providers.py:86-100`) and the agent-event plus
  pty/tmux output callbacks
  (`runner_broadcasting.py:113-231`). Fix: construction
  failure is transactional while resolved ownership is held
  — `GobbyRunner.__init__` catches a failing stage and rolls
  back every construction-owned resource and process-global
  its completed stages installed, in reverse stage order
  (callbacks when installed, published app context,
  span-exporter registration via `shutdown_providers`,
  `DatabaseExecutor` shutdown-and-join, hub database close),
  re-raising only after rollback completes; `run_gobby`
  releases its claim only after that rollback-complete
  propagation, and `main()`'s idempotent `finally` covers
  main-owned claims after the same rollback. Regression
  tests replace reliance on the pre-resource shape: injected
  stage failures after database-and-executor creation and,
  separately, after `set_app_context` at `HTTPServer`
  construction each prove a contender contends until
  rollback completes, no executor thread, open hub pool,
  published app context, or exporter registration survives,
  and a subsequent same-interpreter lifecycle acquires the
  claim and starts cleanly; the missing-config shape is
  retained. `src/gobby/runner_broadcasting.py` joins
  1.4.20's file list; the §1.4 serialization note records
  `GobbyRunner.__init__`'s reverse-order rollback and the
  post-rollback failure-path release. Validation passed
  after edits.

**Round 34** `kind: verification`

- reviewer_run: a6bfd07b-d852-447f-8db8-d23468d32fd6
- verdict: needs_review (adversary review round 33; 2 blocking
findings; artifact unmodified by reviewer)
- findings: R33-F1 (completeness, §1.4/1.4.20): the rollback
  inventory omitted construction-owned state from the middle
  stages — the services stage builds the memory stack whose
  `MemoryManager` constructor creates a Falkor client
  (`runner_init/services.py:117-173`,
  `memory/manager.py:66-193`) with asynchronous teardown
  (`memory/manager.py:309-316`; awaited at
  `runner_lifecycle_shutdown.py:523-539`), the orchestration
  stage installs tmux module globals
  (`runner_init/orchestration.py:136`,
  `agents/tmux/__init__.py:59-67`), and `HTTPServer`
  construction publishes tool-summarizer module globals
  (`servers/http.py:237-239`,
  `utils/tool_summarizer.py:25-35`) — none restored by the
  fixed callback/app-context/telemetry/executor/hub-pool
  list, and the synchronous rollback had no path for the
  async teardown. R33-F2 (lifecycle-state, §1.4/1.4.20):
  `shutdown_providers` is not a same-interpreter rollback —
  `init_telemetry` (`runner_init/storage.py:148`) installs
  tracer and meter providers through one-shot OpenTelemetry
  API setters (`telemetry/__init__.py:95-101`) that reject
  replacement and keep returning the shut-down provider, so
  rollback-by-shutdown strands live instrumentation on a
  dead provider while a successor attaches its exporter to a
  shadow non-global provider; health-metric state
  (`telemetry/__init__.py:107,138-141`) was not rolled back
  either.
- resolution_notes: every citation verified against source.
  Fix: the hand-maintained reverse-order list is replaced by
  a construction rollback ledger owned by
  `GobbyRunner.__init__` — each init stage appends an undo
  entry immediately after each rollback-relevant install
  (hub database, `DatabaseExecutor`, lifecycle-owned
  telemetry attachments, memory stack, tmux helpers,
  published app context, summarizer globals, output
  callbacks), and a failing stage unwinds the ledger
  last-in-first-out, driving asynchronous entries
  (`MemoryManager.close`, `VectorStore.close`) with
  `asyncio.run` under normal shutdown's five-second bounds,
  re-raising after the unwind. Telemetry now rolls back by
  ownership: OpenTelemetry API providers and LLM
  instrumentors are interpreter-latched (installed once,
  never torn down; provider acquisition reuses an installed
  API-global provider instead of constructing a shadow),
  while the lifecycle-owned `SpanStorage` span processor is
  shut down with its latch reset and health metrics are
  disabled via `configure_health_metrics(enabled=False)`.
  New resets land in `agents/tmux/__init__.py` and
  `utils/tool_summarizer.py`; injected-failure regressions
  extend to after memory-stack initialization, after
  `configure_tmux`, and after `set_app_context` past the
  summarizer publish, each also pinning that the
  OpenTelemetry API global still returns the live provider
  and that a successor lifecycle's emitted span reaches its
  own `SpanStorage`.
  `runner_init/{storage,services,servers}.py`,
  `agents/tmux/__init__.py`, `utils/tool_summarizer.py`,
  and `telemetry/providers.py` join 1.4.20's file list; the
  §1.4 serialization note records the ledger. Per user
  direction this is the final adversary round; the plan
  proceeds to expansion without a further review spawn.
  Validation passed after edits.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Add strict persistence, registration outcome, and scoped removal to the subscription
    helpers
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: subscribe_agent_completion
  labels:
  - covers:event-driven-wait-for-agent:1.1:1.1.1
  - covers:event-driven-wait-for-agent:1.1:1.1.2
  - covers:event-driven-wait-for-agent:1.1:1.1.3
  - covers:event-driven-wait-for-agent:1.1:1.1.4
  - covers:event-driven-wait-for-agent:1.1:1.1.5
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Rewrite wait_for_agent as subscribe-and-return
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: wait_for_agent
  labels:
  - covers:event-driven-wait-for-agent:1.2:1.2.1
  - covers:event-driven-wait-for-agent:1.2:1.2.2
  - covers:event-driven-wait-for-agent:1.2:1.2.3
  - covers:event-driven-wait-for-agent:1.2:1.2.4
  - covers:event-driven-wait-for-agent:1.2:1.2.9
  - covers:event-driven-wait-for-agent:1.2:1.2.10
  - covers:event-driven-wait-for-agent:1.2:1.2.11
  - covers:event-driven-wait-for-agent:1.2:1.2.5
  - covers:event-driven-wait-for-agent:1.2:1.2.6
  - covers:event-driven-wait-for-agent:1.2:1.2.7
  - covers:event-driven-wait-for-agent:1.2:1.2.8
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: Acknowledged terminal delivery and sweep redelivery
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  validation_criteria: AgentCleanupHandler.notify_terminal_completion
  labels:
  - covers:event-driven-wait-for-agent:1.3:1.3.1
  - covers:event-driven-wait-for-agent:1.3:1.3.2
  - covers:event-driven-wait-for-agent:1.3:1.3.3
  - covers:event-driven-wait-for-agent:1.3:1.3.4
  - covers:event-driven-wait-for-agent:1.3:1.3.5
  - covers:event-driven-wait-for-agent:1.3:1.3.6
  - covers:event-driven-wait-for-agent:1.3:1.3.7
  - covers:event-driven-wait-for-agent:1.3:1.3.8
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: Close the completion-subscription leak on bypass terminal paths
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  - '1.3'
  validation_criteria: _complete_self_terminated_run
  labels:
  - covers:event-driven-wait-for-agent:1.4:1.4.1
  - covers:event-driven-wait-for-agent:1.4:1.4.2
  - covers:event-driven-wait-for-agent:1.4:1.4.3
  - covers:event-driven-wait-for-agent:1.4:1.4.4
  - covers:event-driven-wait-for-agent:1.4:1.4.5
  - covers:event-driven-wait-for-agent:1.4:1.4.6
  - covers:event-driven-wait-for-agent:1.4:1.4.7
  - covers:event-driven-wait-for-agent:1.4:1.4.8
  - covers:event-driven-wait-for-agent:1.4:1.4.9
  - covers:event-driven-wait-for-agent:1.4:1.4.10
  - covers:event-driven-wait-for-agent:1.4:1.4.11
  - covers:event-driven-wait-for-agent:1.4:1.4.12
  - covers:event-driven-wait-for-agent:1.4:1.4.13
  - covers:event-driven-wait-for-agent:1.4:1.4.14
  - covers:event-driven-wait-for-agent:1.4:1.4.15
  - covers:event-driven-wait-for-agent:1.4:1.4.16
  - covers:event-driven-wait-for-agent:1.4:1.4.17
  - covers:event-driven-wait-for-agent:1.4:1.4.18
  - covers:event-driven-wait-for-agent:1.4:1.4.19
  - covers:event-driven-wait-for-agent:1.4:1.4.20
  - covers:event-driven-wait-for-agent:1.4:1.4.21
  tdd: true
  source_section: '1.4'
  implementation_domain: backend
- title: Remove wait_for_agent from wrapper wait-tool handling
  category: code
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: src/gobby/mcp_proxy/wait_tools.py
  labels:
  - covers:event-driven-wait-for-agent:2.1:2.1.1
  - covers:event-driven-wait-for-agent:2.1:2.1.2
  - covers:event-driven-wait-for-agent:2.1:2.1.3
  tdd: true
  source_section: '2.1'
  implementation_domain: backend
- title: Rework merge orchestration for wake-driven waits
  category: config
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: src/gobby/install/shared/workflows/agents/merge-orchestrator.yaml
  labels:
  - covers:event-driven-wait-for-agent:3.1:3.1.1
  - covers:event-driven-wait-for-agent:3.1:3.1.2
  - covers:event-driven-wait-for-agent:3.1:3.1.3
  - covers:event-driven-wait-for-agent:3.1:3.1.4
  - covers:event-driven-wait-for-agent:3.1:3.1.5
  tdd: true
  source_section: '3.1'
  assigned_agent: backend-developer
- title: Update coordinator, goal, and plan guidance
  category: config
  task_type: feature
  depends_on:
  - '3.1'
  validation_criteria: src/gobby/install/shared/skills/build-coordinator/SKILL.md
  labels:
  - covers:event-driven-wait-for-agent:3.2:3.2.1
  - covers:event-driven-wait-for-agent:3.2:3.2.2
  - covers:event-driven-wait-for-agent:3.2:3.2.3
  - covers:event-driven-wait-for-agent:3.2:3.2.4
  - covers:event-driven-wait-for-agent:3.2:3.2.5
  - covers:event-driven-wait-for-agent:3.2:3.2.6
  tdd: true
  source_section: '3.2'
  assigned_agent: backend-developer
- title: Update MCP tool documentation
  category: docs
  task_type: feature
  depends_on:
  - '1.2'
  validation_criteria: docs/guides/mcp-tools.md
  labels:
  - covers:event-driven-wait-for-agent:3.3:3.3.1
  tdd: false
  source_section: '3.3'
  assigned_agent: tech-writer
```
