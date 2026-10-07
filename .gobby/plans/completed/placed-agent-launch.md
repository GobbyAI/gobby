Plan artifact: `.gobby/plans/placed-agent-launch.md`

# Placed agent launch: spawn_agent into a named gclient tab or split

**Plan ID:** placed-agent-launch

## Overview
`kind: framing`

Task #22904 sits under epic #22691 (tier 3: runbooks as ordinary pipelines, #22895).

A runbook is an ordinary pipeline whose `mcp` steps call `gobby-agents:spawn_agent`.
The only primitive missing is launching an agent into a chosen gclient tab or split
and learning where it landed. This plan adds that primitive to `spawn_agent` itself, so
that every existing guard, lease, isolation path, sandbox wrap and caller-project
authority still applies. That holds whether the caller is a session, a pipeline `mcp`
step, the CLI or cron.

Placement runs in two steps. A side-effect-free preflight runs before isolation is
created, so a refused preflight spawns nothing. The guarded reservation runs later, at
dispatch. By then every existing guard has passed and the isolation, child session and
agent run exist, but no terminal row does. A reservation that fails or is cancelled
removes its pane before it returns, and the existing spawn cleanup removes what
dispatch created. If that removal itself fails, the unbound pane is left for the next
workspace sweep to prune. The
reserved pane is bound to the agent's terminal while that terminal is still `pending`,
which is before provider exec, so a placed agent never exists unplaced. The pane stays
in flight until the launch settles, so no workspace close, move or swap can act on it
before the agent is placed or rolled back. A launch terminal whose kill fails stays
`orphaned` in its pane, so the seat stays occupied until the terminal is settled.

## Constraints
`kind: framing`

- Runbooks are ordinary pipelines (#22895). There is no runbook schema, service,
  deployment ledger, migration or runbook-specific API. Generic parallel fan-out is
  not required.
- Placement is a `spawn_agent` input. A pipeline reaches it through an existing `mcp`
  step. Guards, leases, isolation and caller-project authority are preserved.
- Validation happens before side effects: a refused preflight spawns nothing. A
  reservation refused at dispatch starts no agent, and the existing spawn cleanup
  removes what dispatch created.
- Each agent definition declares its own network policy (decision 14).
  `spawn_agent` gains no sandbox input, and no definition disables SRT.
- This planning task contains no code and no live spawning.
- Josh's acceptance (via PD #14610, 2026-09-26; scope clarified the same day):
  - Every agent that `spawn_agent` launches, from a pipeline or the daemon, placed
    or not, runs inside managed SRT before provider exec, with no unsandboxed
    fallback (decision 13).
  - Placement preserves canonical SRT wrapping, policy, grants, leases, isolation and
    resume.
  - Any invalid input or wrap failure is refused before any unplaced or unsandboxed
    agent side effect.
  - The isolated acceptance tests include a two-seat tab+split placement fixture (an
    ordinary pipeline owned by the tests) and a live-seat refusal.
  - gclient hand launches and plain shells have no sandbox mechanism and are out of
    scope. Current hand-launched seats are not relaunched.
- Josh's rulings of record:
  - Spawned agents already run in gclient panes, and plans keep the as-is state
    separate from the to-be design (memory 556ec801).
  - `spawn_agent` creates the gclient pane itself (memory 7faa183d, constraint 1).
    This plan delivers that constraint and nothing else from the parked meeseeks
    lifecycle.
- Monolith ceiling: on main `cb4f91b49b` these targets are at or above 850 lines:
  - `_implementation.py`: 964
  - `spawn_executor.py`: 962
  - `workspace_ops.py`: 893 on 0.5.0 `070a19c3d4`, after #22883's pane-access
    extraction (`workspace_pane_access.py`), which 1.5 builds on and follows
    with the pane I/O move
  - `storage/workspaces.py`: 973
  - `resume_executor.py`: 972

  New logic lands in new modules. Each deliverable that touches one of these files
  names its move.

## Decision Record
`kind: framing`

1. **Input shape.** Add `spawn_agent(placement=...)`, where `placement` is either of:

   ```text
   {"tab": {"workspace": <workspace ref>, "title": <seat label>}}
   {"split": {"pane": <pane ref>, "axis": "right"|"down", "title": <seat label>}}
   ```

   - `title` is required and is the seat label. It becomes the tab title for `tab`, or
     the pane label for `split`.
   - Refs use the existing workspace reference grammar (`node:workspace:tab:pane`,
     `workspace_contract.py`).
   - Rejected: a separate `seat` field or column. The label already exists on
     `workspace_tabs.title` and on the pane label, so a new field would need a schema
     change and adds no power.
2. **Validate, then reserve, then spawn.** Placement preflight is side-effect free and
   runs before worktree or clone isolation is created, which happens early today at
   `spawn_agent_impl`. The preflight covers:
   - ref resolution;
   - actor scope (`ActorScope.admits` for the caller's project);
   - node match;
   - the live-seat check;
   - the SRT requirement.

   Only after every existing guard passes does the pane reservation insert a row. The
   existing guards are: parent session, `can_spawn` depth, task context,
   `active_task_response_if_blocked`, `TaskSpawnLease`, and `reserve_agent_slot`. The
   reserved row is an in-flight pane (terminal NULL), or an in-flight tab with its
   first pane.

   Rejected: preflighting after isolation. That leaves worktrees behind for refused
   placements.
3. **Bind before exec.** The reserved pane is bound with
   `set_pane_terminal(owns_terminal=True)` to the terminal row that
   `TerminalManager.create_pending` creates in the spawn executor. The placed-launch
   order is the as-is order, VERIFIED by R2 #14640, with one new step (5):
   1. `_prepare_provider_sandbox`;
   2. `prepare_sandbox_launch` (SRT policy plus a real `--preflight`);
   3. `wrap_provider_command`;
   4. `create_pending`;
   5. bind (new, placed launches only);
   6. `reserve_observer` / `prepare_spawn`;
   7. provider exec.

   Binding therefore follows a successful SRT wrap and precedes exec. A wrap failure
   never creates a terminal row, so only the reservation needs releasing.
   - Rejected: adopting after launch via `tab_create`/`pane_split(terminal_id=...)`.
     `WorkspaceOps._adoptable` requires `state == "live"`, which means the agent would
     run unplaced first, and gclient's `created` handler would already have opened it
     unplaced. That violates Josh's acceptance.
4. **Synchronous result for placed launches.** When `placement` is set, `spawn_agent`
   awaits the spawn phase (`_execute_spawn_phase`) inline instead of scheduling it in
   the background.
   - Success returns the full `build_spawn_response` payload plus `workspace`,
     `tab_ref` and `pane_ref`.
   - A wrap, provider or bind failure returns `success: false` with a typed
     `placement_error` or the existing spawn error.

   A pipeline `mcp` step therefore fails deterministically, and a later split step can
   use `${{steps.<id>.output.pane_ref}}`. Unplaced spawns keep today's background
   behaviour.
5. **Compensation.** The placed branch runs in `run_placed_spawn` (`_placement.py`),
   which has two phases.
   - Reserve. `reserve` either returns a reserved pane or compensates before it
     raises (1.1). Successful compensation leaves no pane or tab row, no in-flight
     mark and no seat entry. A failed rollback leaves only an unbound row that the
     next `sweep_dead_panes` prunes. `run_placed_spawn` wraps the
     call in the same exits that `_execute_spawn_phase` uses. A typed refusal and any
     other exception return through `_spawn_failure`, and a `CancelledError` runs
     `_spawn_failure` and is re-raised. `_spawn_failure` runs `cleanup_failed_spawn`,
     which removes the run, the child session and created isolation. There is no pane
     to release.
   - After reserve. The compensation boundary opens when `reserve` returns and closes
     with the final response. The pane is retained only when that response has
     `success: true`. Every other exit releases the reservation in a `finally`, so
     release still runs when cleanup raises. The exits are:
     - a returned `success: false`. `finalize_executed_spawn` (`_execution.py`)
       reports provider and SRT preparation failure, terminal liveness failure,
       start-run failure and auto-claim failure this way, after it has already run
       `cleanup_failed_spawn`;
     - an exception or `CancelledError`, which `_execute_spawn_phase` routes to
       `_spawn_failure`, which runs `cleanup_failed_spawn`;
     - a bind conflict, which the executor turns into a failed `SpawnResult` (1.2).

   The boundary reuses those existing cleanups and adds only the release, so
   `cleanup_failed_spawn` runs exactly once on every failure exit.
   - Terminal kill has one owner on every failure exit: `cleanup_failed_spawn`, run
     at most once per spawn attempt through the attempt's `SpawnCleanupOnce` (1.6).
     Its steps run independently and never raise, so a failed step never skips a
     later one. Its terminate step settles the terminal `exited` only after a kill
     succeeds. A kill that fails moves the row from `pending` or `live` to
     `orphaned` (`mark_kill_failed`, 1.6), and cleanup then keeps created
     isolation, because the process may still run there. A terminal held
     in-doubt (1.9) is not killed by cleanup; its created-isolation removal is
     deferred to the claim's owner, which runs it only after a proven settlement.
   - Release runs after cleanup, from the launch terminal id the binder recorded
     before bind ran, and it kills before it removes (1.1). A terminal that is
     absent or already inactive needs no kill, and the pane is removed. A terminal
     that is still `pending` or `live` is killed as a backstop; on success the pane
     is removed. A terminal that is `orphaned`, held in-doubt (1.9), or whose
     backstop kill fails, keeps its pane: release binds it to that terminal if bind never ran and clears the
     in-flight mark. The pane is the seat's workspace association, so the seat
     stays occupied (decision 7), the terminal stays listed, and `terminal_kill`
     retries it. After a successful kill the row is `exited`, the next
     `sweep_dead_panes` removes the pane, and the seat is free again. No terminal
     is killed twice and no live terminal outlives its pane unlisted.
   - Placed launches default `cleanup_isolation_on_failure` to true for isolation that
     this call created. As-is, the default is false (`spawn_agent_impl`,
     `_failure_cleanup.py`), which leaves a worktree behind after a refused seat.
     Reused worktrees and clones (`worktree_id` / `clone_id`) are never removed.
6. **Managed SRT is mandatory for placed launches.** Preflight refuses with
   `placement_error: "sandbox_required"` unless the effective sandbox config is
   `enabled` with `backend == "srt"`. The config checked is the daemon-owned
   `agent_sandbox_config` after `apply_write_grant`, or the managed runtime profile's
   config. Preflight also runs the side-effect-free `verify_srt_installation`.
   - The full SRT policy and `--preflight` need the isolation cwd and the run_id, so
     they stay where they are. VERIFIED by R2: `_prepare_provider_sandbox` catches
     `OSError`/`ValueError`/`SrtRuntimeError`, fails the run, and returns "Sandbox
     startup failed closed" before `create_pending`. Placed launches reuse that path
     unchanged, and its failure releases the reservation.
   - Unplaced spawns and resumes get the same requirement in 1.8 (decision 13).
     Hand-launched seats are out of scope.
7. **Live-seat refusal.** The seat key is `(workspace_id, truncate_title(title))`
   for both placement kinds. Preflight refuses with `placement_error: "seat_live"`
   when the target workspace already holds a tab title or a pane label equal to the
   canonical title whose bound terminal is `pending`, `live` or `orphaned`. Both kinds
   are checked
   for both variants, so a tab seat and a split seat cannot share a title in one
   workspace. Two titles that truncate to the same stored value are one seat.
   Re-running a runbook pipeline therefore cannot double-launch a seat. A seat whose
   terminal has ended can be relaunched.
   - An in-flight reservation with the same title also counts as live.
   - Preflight's seat check is repeated inside `reserve` under a per-workspace
     `asyncio.Lock` held across check and insert, so two concurrent launches of one
     seat cannot both pass. The daemon is a single process, and the in-flight pane set
     is per process too (VERIFIED by R2).
   - As-is, the only spawn idempotency key is `task_id` (`_spawn_guards.py`: the
     `RuntimeDispatchMutex` and the active-run checks are taken only when `task_id` is
     set). `AgentRun` has no seat, role or project field. Taskless seats had no guard
     until this check.
   - A runbook seat is by definition a placed launch, because `title` is required.
     Unplaced spawns are not seats and keep today's task-keyed guards. The seat key
     includes the workspace, so the per-workspace lock serializes every launch of one
     seat. `_adoptable` / `_refuse_held` only stop re-adopting an occupied terminal;
     they are not a seat guard and are not relied on.
   - An `orphaned` terminal holds its seat because its kill failed and its process
     may still run. Its pane survives release and `sweep_dead_panes` (decision 5,
     1.5), and the seat frees once `terminal_kill` or a `pane_close` retry settles
     the terminal `exited`. A user's `pane_close`, `tab_close` or `workspace_close`
     keeps today's remove-then-kill order, so closing works when a host is gone; a
     failed kill there leaves the terminal `orphaned` and listed, and the seat free
     by that explicit user action. A pane whose bound terminal is held in-doubt
     (1.9) is the exception: the storage guard refuses its close, move or swap
     `busy` inside the mutation transaction until the owner settles it, because
     a late prepare may still create its session.
   - Refusal precedence for a duplicate placed request: preflight runs first, so a
     request for an occupied seat is `seat_live`. A request for a free seat whose
     `task_id` already has an active run passes preflight and is refused
     `task_active` at the existing task guard, after isolation (1.4). A placed
     request never gets the skipped `success: true` reply without refs.
   - Rejected: keying the seat on (project, agent_name). One agent definition
     legitimately fills several seats (for example two developer lanes), and
     `AgentRun` would need a project join and a new mutex key.
8. **Parent identity.** A placed spawn from a pipeline is parented to the pipeline's
   child session. That session is created by `PipelineExecutor` with
   `source="pipeline"`, the run's `project_id` and `parent_session_id=caller`, and
   `_inject_agent_parent_session_argument` fills in `parent_session_id` for `mcp` steps.
   The pipeline caller depends on how the run started:

   | Entry point | Caller |
   | --- | --- |
   | CLI or HTTP | the system session |
   | MCP `run_pipeline` | the calling session |
   | cron | the cron session |

   - The project is resolved from the parent session by
     `_resolve_spawn_project_context`. The workspace actor is the parent session id.
     VERIFIED by R2: the CLI takes the project from the cwd's `.gobby/project.json`;
     cron takes it from `job.project_id`.
   - No new identity mechanism is added.
   - Fail-closed gap: when child-session registration fails, `PipelineExecutor` falls
     back to the caller session. For CLI runs that is the system session, and the
     project may then resolve from ambient context. A placed spawn refuses with
     `placement_error: "parent_unresolved"` when its parent is the system session
     itself, or when the project did not come from an explicit `project_path` or the
     parent session.
   - That provenance is carried from the factory. `_factory.py` computes one
     `project_context_authoritative` boolean in the same resolution that selects the
     agent definition. It is true when the context came from an explicit
     `project_path` or from the resolved parent session, and false for the ambient
     cwd fallback. It is never inferred from a non-null normalized path. No refusal
     for `parent_session_id == system_session_id()` exists in `spawn_agent` today
     (verified on 0.5.0). 1.4 adds it as a separate explicit check for placed spawns
     only (1.4.8); unplaced spawns keep their current behavior.
9. **Seat spawn blocks are policy, not code, today.** No bundled rule blocks
   `spawn_agent` by seat. The blocks live in role files, and #22899 /
   `agent-definition-profiles.md` plan their enforcement.
   - Decision: running a pipeline is not a bypass. A seat forbidden from `spawn_agent`
     is equally forbidden from starting a spawning pipeline through `run_pipeline`.
   - When enforcement lands (outside this plan), it keys on the originating
     non-pipeline session in the pipeline child's parent chain. It must be in-tool or
     at `run_pipeline`, because pipeline `mcp` steps call the proxy with
     `enforce_workflow=False` and skip the rule engine (VERIFIED by R2:
     `workflows/pipeline/handlers.py`, `tool_execution.py`).
   - Runbook pipelines start through Josh's model: the operator asks the
     Assistant, and the Assistant starts the pipeline.
10. **gclient reconciliation.** gclient must not show a placed agent twice or unplaced.
    - A `created` event for a terminal that a known pane already holds opens nothing
      new. This is today's behaviour in `apply_event`.
    - When a `pane.added` / `tab.created` event binds a terminal that gclient opened
      unplaced, gclient moves the terminal into that pane and drops the unplaced
      surface.

    This covers any ordering of the workspace stream and the lifecycle stream, and a
    client that starts or reconnects after the agent is already bound and live. It
    needs no change to daemon event payloads.
11. **Deferred elsewhere, not here:**
    - closing a pane when its agent ends (`end_agent_run` closes pane and terminal,
      memory 7faa183d, constraint 4);
    - the idle TTL and lifecycle declarations;
    - lane queues;
    - `dispatch_batch` retirement with `gobby build`.

    All of these belong to #22691 siblings.
12. **Plan Adversary round repairs (2026-09-27).** Josh approved design `1d3d4f25a3`;
    the Plan Adversary (gobby#14579) then raised PAL-01 to PAL-09, and the Program
    Director ruled PAL-09 Option A. The repairs reverse two earlier statements:
    - Reversed: "`workspace_ops.py` and `storage/workspaces.py` stay unchanged".
      An in-memory in-flight check read before a separate storage write cannot stop
      a concurrent close, move or swap. 1.5 adds the check inside the storage
      transactions, under the same row locks the inserts take, and routes every
      guarded `WorkspaceOps` mutation through it.
    - Reversed: "cleanup raises, then release kills". `cleanup_failed_spawn` becomes
      one owned, cancellation-safe attempt whose steps never raise (1.6), and a
      failed kill is `orphaned`, never `exited`. This changes the shared failure
      cleanup for unplaced spawns too.
    - Placed timeout: a placed launch whose `timeout_seconds` expires keeps its
      pending terminal `pending` under the in-doubt claim and returns
      `spawn_timeout`; the owner then settles the row in whatever state it holds
      (1.2). The reply
      is bounded by the caller's own timeout, with no dependency on #22663.
    - Placed resume (PAL-09, Program Director Option A): a resumed placed agent is
      re-placed through the same preflight, reserve and bind against current state
      (1.7). A persisted placement is input, never authority, and there is no
      unplaced fallback.
    - #22883 (owned-shell workspace ops, gobby#14642) owns the extraction of the
      pane-source policy from `workspace_ops.py` into `workspace_pane_access.py`
      (`WorkspacePaneAccess`: scope, admission, source selection, adoption, held-
      terminal refusal), landed on 0.5.0 as `070a19c3d4`. 1.5 consumes that
      extraction and does not repeat it. The guarded methods it changes
      (`workspace_close`, `tab_close`, `tab_move`, `pane_swap`, `pane_move`,
      `pane_close`, `_closing`, `_db_guarded`, `_kill`) stay in
      `workspace_ops.py`. The file is still 893 lines after the extraction, so
      1.5 also moves the pane I/O group, which it does not change, into
      `workspace_pane_io.py`.

13. **Every `spawn_agent` launch runs under managed SRT (2026-09-27).** Josh's final
    ruling, relayed by the Program Director (gobby#14610) and replacing every
    earlier interpretation: "A pipeline opens windows using spawn_agent. These are
    always sandboxed." and "gclient itself doesn't sandbox".
    - Scope: every agent `spawn_agent` launches, from a pipeline or the daemon, placed
      or not, and every resume of such an agent. gclient hand launches and plain
      shells are out of scope, and no current seat is relaunched. The Assistant is
      hand-launched, so it needs no exemption mechanism.
    - As-is, two unsandboxed fallbacks exist: `agent_sandbox.enabled` is
      operator-settable (`agent_sandbox_config` only defaults it to true), and
      `backend: provider-native` is a debug override. A resume whose snapshot has no
      sandbox config launches with `SandboxLaunch(backend="provider-native",
      enforced=False)`. 1.8 refuses all three with the typed `sandbox_required`
      error; nothing is downgraded.
    - Cross-plan: the #22899 design (an unsandboxed `research` profile, then named
      profiles) is superseded by decision 14. A definition declaring
      `network: trusted` gets SRT with the Anthropic Trusted-domain seed plus the
      Gobby hosts (provider API domains, the api_base host and loopback, already
      computed by `sandbox_policy.allowed_domains`). Every other definition keeps
      those hosts only and stays least-privilege.
      The seed is a vendored data file that
      records its source URL
      (https://code.claude.com/docs/en/cloud-environments#default-allowed-domains),
      fetch date and sha256, refreshed only by a manual command that prints a diff
      and lands through review; the daemon never fetches it. P4 delivers it
      (decision 14).
    - The Trusted list is a developer-dependency allowlist, not web-research
      coverage: it has no search engines or general research sites. The cloud
      environment's bypasses (a separate GitHub proxy, MCP connectors through
      Anthropic's servers) are cloud-proxy properties and are not assumed of local
      SRT.
    - Web transport for a `trusted` agent, option A: provider-native search runs server-side at
      the provider (INFERRED); `brave-search` and `context7` run as daemon MCP
      children outside the agent sandbox, reached over the loopback MCP proxy under
      Gobby rules. Named gap: local WebFetch to a host outside the allowlist is
      blocked (INFERRED; the V1 smoke confirms it). `playwright` and
      `chrome-devtools` can navigate anywhere, so rules deny them to spawned agents
      unless the definition allows them. A scoped research-host list or a governed
      daemon fetch tool is added only if the smoke shows option A is insufficient,
      and that is a product decision returned through the Program Director. There is
      no allow-all fallback: `render_srt_settings` keeps `strictAllowlist` and
      `_raise_srt_lockout` refuses unrestricted network.
    - Loopback is allowed. #22961 landed at `08e54acb95` and `2c2b569fe3`, so a
      sandboxed agent's MCP call through daemon REST or `gobby mcp-proxy call-tool`
      is rule-enforced, and 1.8.5 verifies it.
    - Credential boundary. VERIFIED denied: `_credential_roots` (`bootstrap.yaml`,
      `.secret_kek`, `local_cli_token`, `tools/srt`) and `_TOOLCHAIN_CREDENTIAL_PATHS`.
      A spawned agent reaches daemon REST only with its identity-bound agent token.
      Provider auth stays readable because the provider needs it.
    - Design findings, recorded for the tightening Josh schedules closer to launch
      and not in any leaf here (Program Director ruling, 2026-09-27):
      - Credential hardening (recommended): a spawned agent can still read
        `~/.config/gh` and `~/.ssh`, and git may consult the macOS keychain
        credential helper, so it can act on GitHub as Josh. Denying those reads and
        clearing the helper in the wrapped environment would close that.
      - Escape surface: the security goal also forbids a sandboxed agent creating or
        driving an unsandboxed shell through MCP, daemon REST, gterm or gclient. The
        Researcher (gobby#14550) is auditing those paths; its findings return
        through the Program Director.
    - Trust statement: SRT bounds a spawned agent's network and filesystem. Any
      credential it can still read acts with full authority on allowlisted hosts.
      #22961 closes only the daemon REST/CLI bypass.
    - Rollout: no migration. #22961 is live (restart of 2026-09-27). After this
      plan lands, the Program Director restarts the daemon under the normal announcement and
      quiet-window protocol, and only new spawns pick up the policy.
14. **Each agent definition owns its network policy (2026-09-27).** Josh, on
    `9fc9c635b1`, relayed by the Program Director: "Don't all agents use their
    agent profile? Just sounds redundant. Researchers aren't special. Some agents
    get network and some don't." This replaces the named-profile design folded in
    from #22899.
    - The agent definition is the agent's profile. `AgentDefinitionBody` gains
      `network: Literal["none", "trusted"] = "none"`, and every definition,
      researcher or not, declares its own value the same way. There is no
      `agent_sandbox.profiles` registry, no `sandbox_profile` selector and no
      research profile.
    - `none` adds nothing to the daemon's `agent_sandbox` policy, which is
      today's policy: the provider API domains, the `api_base` host, loopback to
      the daemon, and any domains the operator lists in
      `agent_sandbox.allowed_domains`. Provider transport and the control plane
      therefore keep working.
    - `trusted` adds the vendored Trusted seed and the existing git and package
      registry groups to that allowlist. It is still SRT with `allow_network`
      false. No value disables the sandbox or opens
      unrestricted network, and `srt_runtime` refuses `allow_network` true.
    - Network permission is separate from the sandbox: decision 13 keeps SRT
      mandatory for every value, and 1.8 refuses any config that is not enabled
      SRT.
    - Only the bundled template sync writes `trusted` (4.2). #22965 landed at
      `b024c6e096` (activation pending): once active, the definition MCP tools
      and HTTP routes refuse agent-token mutation requests for every field. That
      endpoint actor check is separate from this plan's storage guard, which
      does not depend on it.
    - Spawn resolves the field inside the 1.8 gate (4.3). Resume replays the
      spawn-time snapshot, so a definition edit applies to later spawns.
    - Assignment: this plan changes no bundled definition, so every agent stays
      `none` when it lands. Which definitions declare `trusted` is each
      definition's own content. #22902 owns the seat definitions; its researcher
      seat is the first candidate, and Josh decides it there.
    - No per-definition domain list: no concrete requirement exists, and the enum
      keeps every widening reviewed through the bundled templates.
    - Trust limitation, for the approval summary: any agent or worker allowed to
      spawn a `trusted` definition gets a child with that egress. This plan adds
      no new capability or credential boundary. Decision 13's trust statement
      applies unchanged.

## Evidence (as-is, VERIFIED unless marked)
`kind: framing`

- **spawn_agent MCP signature:** `create_spawn_agent_registry` in
  `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py`. Parameters: prompt, agent,
  task_id, allow_closed_task, isolation, branch_name, base_branch, clone_id,
  worktree_id, cleanup_isolation_on_failure, workflow, provider, model,
  reasoning_effort, reasoning_required, timeout, parent_session_id, project_path,
  notify_parent_on_completion, terminal_backend, droid_mode, extra_write_paths,
  write_paths_reason, reserved_run_id. There is no placement or sandbox input.
- **Response:** the immediate MCP reply is `status: "starting"` with run_id,
  child_session_id and isolation fields, returned before any terminal exists.
  `build_spawn_response` (`_response.py`) adds pid, terminal_id and backend in the
  background finalize step. No tab_ref or pane_ref exists anywhere.
- **Guard order in `spawn_agent_impl`:**
  1. parent session required, then `can_spawn`;
  2. `resolve_spawn_task_context`;
  3. the sandbox config is resolved;
  4. isolation is created;
  5. `active_task_response_if_blocked`;
  6. `TaskSpawnLease.acquire`;
  7. `reserve_agent_slot` (`_spawn_guards.py`, per-project lock);
  8. the background `_run_spawn_phase`.

  Exceptions and cancellation in `_execute_spawn_phase` route to `_spawn_failure`,
  which calls `cleanup_failed_spawn` (`_failure_cleanup.py`) with these steps: record
  error, kill terminal, terminalize the run, clean up isolation, delete the child
  session. Typed failures do not raise. `finalize_executed_spawn` (`_execution.py`)
  runs `cleanup_failed_spawn` itself and returns `success: false` for a failed
  `SpawnResult` (including provider and SRT preparation failure), terminal liveness
  failure, start-run failure and auto-claim failure. `reserved_run_id` is only for
  the close reviewer; it is not an idempotency key. Task retries dedupe through
  `task_spawn_lease`.
- **Terminal path:** `execute_spawn` calls `_runtime_spawn` (`spawn_executor.py`), which
  calls `TerminalManager.create_pending`, then native `reserve_observer`, then
  `prepare_spawn`, then `_promote_prepared`. The SRT wrap is applied in
  `spawn_executor_providers.py` and `spawn_executor_support.py` through
  `wrap_provider_command`.
- **Sandbox defaults:** `daemon_owned_sandbox_config` in `agents/sandbox.py` defaults to
  `enabled=True`, `backend="srt"`, `allow_network=False`, but the `agent_sandbox`
  config can set `enabled: false` or `backend: provider-native`. `spawn_agent_impl`
  records `sandbox_enabled=False` at session creation. `update_sandbox_enabled`
  (`agents/session.py`) exists for the later update (INFERRED: set after the wrap).
- **Workspace substrate:** `WorkspaceOps.tab_create` and `pane_split`
  (`terminals/workspace_ops.py`) do the following in order:
  1. `_pane_source` checks scope before insert;
  2. `mark_spawn_in_flight`;
  3. insert the row;
  4. `_fill` binds or spawns;
  5. `clear_spawn_in_flight`;
  6. emit `tab.created` / `pane.added`.

  `_fill` rolls back on failure. `_adoptable` requires a live terminal on the
  workspace's node. `WorkspaceManager.set_pane_terminal` (`storage/workspaces.py`) has
  no state check and raises `UniqueViolation` when another pane holds the terminal.
  Tab titles and pane labels exist (`create_tab(title=)`, `rename_pane`).
- **gclient:**
  - `apply_event` (`crates/gclient/src/app/apply.rs`) opens a `created` terminal
    unplaced only when `pane_for_terminal` is `None`.
  - The live path `apply_live_event` (`crates/gclient/src/app/live.rs`) calls
    `ensure_live_pane` on `created`.
  - User spawns are placed client-side by `placement_op`
    (`live_loop/workspace_actions.rs`).
  - The agent `created` broadcast comes from `broadcast_agent_event`
    (`runner_broadcasting.py`), after the terminal exists (INFERRED: after
    promotion).
- **Axis vocabulary:** `WorkspaceManager.add_pane` (`storage/workspaces.py`) accepts
  only `horizontal` and `vertical` (`_axis`). `horizontal` divides the width, so its
  children sit side by side (gclient sizes `LayoutAxis::Horizontal` children by
  width). The placement input `right` therefore maps to `horizontal`, and `down` maps
  to `vertical`.
- **Composition:** `WebSocketServer.configure_terminals`
  (`servers/websocket/server.py`) builds the one `WorkspaceOps` from the workspace
  manager, terminal manager, runtime registry, write coordinator, `SessionManager`
  and `publish=self.broadcast_workspace_event`. MCP tools reach it through
  `workspace_ops_resolver`, which `HTTPServer._init_mcp_subsystems` passes to
  `setup_internal_registries` (`mcp_proxy/registries.py`). The spawn tool chain is
  `setup_internal_registries`, then `create_agents_registry` (`agents_registry.py`),
  then `AgentsRegistryContext` (`agents_context.py`), then
  `register_agent_spawn_tools` (`agents_spawn_tools.py`), then
  `create_spawn_agent_registry`. None of those carries the workspace services today.
- **Project provenance:** `_resolve_spawn_project_context` (`_factory.py`) returns
  `(context, path)`. It can return the parent context with the ambient cwd path, or
  the ambient context alone, and nothing marks which source won.
- **gclient live path:** `apply_live_event` (`app/live.rs`) hands workspace events to
  `apply_live_workspace_event` (`app/live_workspace.rs`). That function applies the
  model change and moves `pending_placements` matches into `placed_panes`.
  `project_live_workspace` (`app/live_loop/projection.rs`) projects the model onto
  Chrome and focuses `placed_panes`. `ensure_live_pane` reuses a pane by terminal id.
  `pane_for_terminal` (`app/workspace_panes.rs`) sees only instantiated panes, while
  `WorkspaceModel::pane_ref_for_terminal` (`app/workspace_ops.rs`) answers from the
  daemon model. Startup and reconnect run `reconcile_subscribe_first` (`app/live.rs`,
  `app/mod.rs`): the workspace snapshot first, then the live roster, then projection.
- **Pipeline identity:** `PipelineExecutor` registers the child session
  (`source="pipeline"`, `project_id`, `parent_session_id=caller`, `agent_depth=0`) and
  falls back to the caller session if creation fails. `_inject_agent_parent_session_argument`
  (`mcp_proxy/services/tool_execution.py`) fills in `parent_session_id` for
  `spawn_agent` calls.
- **Seat blocks:** the only rules naming `spawn_agent` are memory surfacing,
  require-restraint, block-direct-provider-launch and no-agent-spawn-for-merge. None of
  them is a seat block.
- **Prior designs:**
  - `deploy-runbook.md` placement `{tab:{title}} | {split:{slot,axis}}`;
  - `completed/runbooks.md` §1.5 (reserve the pane before the process, reply with
    pane_ref/tab_ref);
  - `completed/gclient-workspaces.md` decision #10 excluded spawn-time placement.

  Josh's ruling of 2026-09-26 (memory 7faa183d) supersedes that exclusion.

## P1: Daemon placement
`kind: framing`

Reservation primitives (1.1), the executor bind hook (1.2), the daemon-scoped
reserver composition (1.3), and the `spawn_agent` placement input that uses them
(1.4).

### 1.1 Agent pane reservation primitives [category: code] (depends: 1.5, 1.6)
`kind: deliverable`

Targets:
- `src/gobby/terminals/workspace_agent_panes.py`
- `tests/terminals/test_workspace_agent_panes.py`

Add a new module, `workspace_agent_panes.py`. It owns the placement lifecycle for
agent panes, so the lifecycle adds nothing to `workspace_ops.py` or
`storage/workspaces.py`. The storage guards it relies on land in 1.5, and the
failed-kill settlement in 1.6. `AgentPaneReserver` is constructed from the same `WorkspaceManager`,
`TerminalManager`, `TerminalRuntimeRegistry`, `SessionManager` (for `ActorScope`) and
event-publish callback that `WorkspaceOps` receives. It holds the per-workspace lock
map and its own map of in-flight seat entries. 1.3 builds the one daemon-scoped
instance. It reuses `_pane_of` (`workspace_contract.py`), `mint_pane_id`,
`mark_spawn_in_flight`, `clear_spawn_in_flight`, `create_tab`, `add_pane`,
`rename_pane`, `set_pane_terminal`, `remove_pane`, `truncate_title`, `kill_terminal`
(`terminals/termination.py`) and `TerminalManager.mark_kill_failed` (1.6).

New API (all names are new):
- `AgentPlacement`: a frozen dataclass parsed from the `placement` input. It has two
  variants:
  - `tab`: workspace ref and title;
  - `split`: pane ref, axis in {`right`, `down`}, and title.

  A missing or empty title, an unknown axis, an unknown key or both variants at once
  each raise `AgentPlacementError("invalid_placement")`. A parsed split carries the
  storage axis: `right` maps to `horizontal` and `down` maps to `vertical`, the only
  values `WorkspaceManager.add_pane` accepts.
- `AgentPaneReserver.preflight(actor, project_id, placement)`: read-only. It runs
  before isolation exists, so it takes no worktree. It does the following:
  - resolves the ref through the workspace resolver (`not_found` / `invalid_ref`);
  - checks that the actor scope admits `project_id` (`forbidden`);
  - requires the workspace's machine to be this node (`invalid_op`);
  - for `split`, requires the target pane's tab project to equal `project_id`
    (`forbidden`);
  - applies the live-seat check on the seat key `(workspace_id,
    truncate_title(title))`. Any tab title or pane label in that workspace equal to
    the canonical title, whose terminal state is in the new `SEAT_HELD_STATES`
    (`pending`, `live`, `orphaned`), gives `seat_live`. Both kinds are scanned for
    both variants.

  It returns an immutable `ResolvedPlacement`, which carries the workspace id, the
  project id and, for `split`, the beside pane's tab id, all as read.
- `reserve(resolved, *, worktree_id)`: under the per-workspace `asyncio.Lock` held
  across check and insert, it repeats the live-seat check on the same key, counting
  in-flight reservations with the same canonical title as live. Then it mints the pane
  id, records the seat entry, marks the pane in flight and inserts it. It uses
  `create_tab` with the title and `worktree_id` for `tab`. For `split` it uses
  `add_pane` beside the pane with the mapped axis and the preflighted
  `expected_workspace_id` and `expected_tab_id` (1.5), so a beside pane or tab that
  moved after preflight refuses `not_found` and inserts nothing. Then it uses
  `rename_pane` for the label.
  `worktree_id` is the finalized isolation association: `None` for isolation none, the
  existing id for a reused worktree, and the new id for a fresh worktree. A split joins
  an existing tab, so it stores no worktree. `reserve` returns
  `ReservedPane(pane_id, tab_id, workspace_id, pane_ref, tab_ref)`. Nothing is emitted
  yet.
- `reserve` is compensated. It returns a `ReservedPane`, or it compensates and then
  raises. When the compensation succeeds, no pane row, tab row, in-flight mark or seat
  entry remains. It is built from existing facilities:
  - The mark is an in-memory set (`WorkspaceManager.mark_spawn_in_flight`), and the
    seat entry lives in the reserver's own map. Both are set on the event-loop thread
    with no await before the insert starts. Both are cleared in a `finally` on every
    exit except a successful return.
  - `create_tab` inserts the tab and its pane in one transaction, and `add_pane`
    inserts the pane and rewrites the layout in one transaction. A failed insert
    therefore leaves no partial row. Only the split label is a second statement.
  - The insert and the label run as one task awaited through `asyncio.shield`, the
    pattern `_runtime_spawn` uses for `prepare_spawn` (`spawn_executor.py`). On
    `CancelledError`, `reserve` first waits for that task to settle, so no commit can
    land after the rollback. Then it rolls back and re-raises.
  - Rollback removes the row by the pre-minted pane id and publishes the removal, as
    `WorkspaceOps._roll_back` does. It tolerates `WorkspaceNotFoundError` when the
    insert never committed, and it runs shielded so a repeated cancellation cannot
    strand it.
  - If the rollback's `remove_pane` itself fails, that row is recoverable residue.
    The mark and the seat entry are still cleared. The row holds no terminal and is
    out of flight, so the seat check ignores it and the next `sweep_dead_panes`
    prunes it. That is the fallback `_roll_back` documents. `reserve` logs the
    rollback failure at WARNING with the phase, the pane id and the exception
    type name only (the sanitized form below), then raises the original error.
- `bind(reserved, terminal_id)`: calls `set_pane_terminal(owns_terminal=True)` and
  emits `tab.created` or `pane.added` with the bound pane. It leaves the in-flight
  mark and the seat entry in place, so a guarded workspace mutation (1.5) refuses
  the pane as `busy` until the launch settles. A `UniqueViolation` or a missing row raises
  `AgentPlacementError("busy"/"not_found")`. A publish failure raises after
  `set_pane_terminal` has persisted the binding, so the pane then holds the
  launch's terminal even though bind raised.
- `release(reserved, *, terminal_id)`: `terminal_id` is the launch terminal that
  the 1.4 binder closure recorded before it awaited `bind`, or `None` when the
  binder never ran or bind refused the terminal as `busy`. `_runtime_spawn` hands
  the binder only the id `create_pending` just returned for this launch, so that
  terminal is the launch's own whether bind succeeded, failed before persisting,
  or persisted the binding and then raised while publishing. Release acts only on
  that id and never reads a terminal id from the pane row, so it never kills a
  terminal that another pane holds. Release kills before it removes, and each step
  runs independently of the others:
  - It reads the launch terminal. When that terminal is `pending` or `live`,
    `kill_terminal` kills it; a kill that fails, including a refusal because the
    terminal is held in-doubt (1.9), calls `mark_kill_failed` (1.6), which moves a
    `live` row to `orphaned` and leaves a `pending` row `pending`, and the pane
    stays.
  - When the terminal is absent, `exited`, or was just killed, `remove_pane` removes
    the pane and a tab it emptied, and the removal events publish. A row that is
    already gone raises `WorkspaceNotFoundError`, which counts as removed.
  - When the terminal is `orphaned`, or `pending` after a failed kill, the pane
    stays. If bind never persisted,
    release binds it to that terminal with `set_pane_terminal(owns_terminal=True)`.
    The pane then holds the seat (decision 7) until the terminal is settled.
  - In a `finally`, the mark and the seat entry are cleared.

  A kill, removal or publish failure therefore never skips the mark clear. Each
  failed step is logged at WARNING with the phase, the pane id, the terminal id
  and the exception type name, and release never raises, so the boundary's reply stays the spawn
  outcome. A row that a failed `remove_pane` leaves behind is out of flight once the
  mark is cleared, and its terminal is inactive, so the next `sweep_dead_panes`
  prunes it. In a placed spawn `cleanup_failed_spawn` has normally already settled
  the terminal, so release issues a kill only when cleanup's terminate step did not
  reach it (decision 5). Release is idempotent.
- `settle(reserved)`: clears the in-flight mark and the seat entry after a
  successful placed reply. The bound pane and its live terminal then hold the seat.

**Granularity:** fifteen acceptance items, but one production file and one lifecycle
owner: the reservation state machine (preflight, reserve, bind, settle, release) of
one agent
pane. The items are that machine's refusals and transitions, and none is closeable
without the others.

**Research context:**
- The reserve-then-fill order mirrors `WorkspaceOps.tab_create` and
  `WorkspaceOps.pane_split` (the in-flight mark protects the unbound pane from sweeps
  and from workspace close). `_fill` / `_roll_back` show the rollback and publish
  semantics to copy, and `pane_close` / `_kill` show the release order and the
  orphan fallback.
- `_adoptable` is deliberately not reused, because it requires a `live` terminal
  (decision 3).
- Seat label: `workspace_tabs.title` via `create_tab(title=)`, and pane labels via
  `rename_pane`. Both are truncated by `truncate_title`, so the seat key uses the
  truncated value.
- Axis: `storage/workspaces.py::_axis` rejects anything but `horizontal` and
  `vertical` (Evidence).
- Errors: reuse the `WorkspaceOpError` codes (`invalid_ref`, `not_found`, `forbidden`,
  `invalid_op`, `busy`) plus the new `invalid_placement`, `seat_live` and
  `sandbox_required` (the last is raised by 1.4) under a new `AgentPlacementError`.
- Rejected: moving preflight after isolation, or adding mutable worktree state to
  `ResolvedPlacement`. The first leaves worktrees behind for refused placements
  (decision 2); the second is not needed once `reserve` takes the final id.
- Planned checks: `DATABASE_URL=<isolated hub> GOBBY_TEST_PROTECT=1 uv run pytest
  tests/terminals/test_workspace_agent_panes.py -v`, plus ruff and mypy on the new
  module.

**Acceptance:**

- 1.1.1 - Invalid placement shapes (no title, bad axis, both variants, unknown key) raise `invalid_placement` without touching storage. test: `tests/terminals/test_workspace_agent_panes.py::test_invalid_placement_shapes_refused`.
- 1.1.2 - Preflight refuses an unknown ref, an out-of-scope project and a foreign node, and inserts no row. test: `tests/terminals/test_workspace_agent_panes.py::test_preflight_refusals_have_no_side_effects`.
- 1.1.3 - Preflight refuses `seat_live` when a tab title or pane label with the same canonical title holds a pending, live or orphaned terminal, across kinds in both directions (an existing tab then a split request, an existing split then a tab request). Two titles that truncate to the same stored value are one seat, and relaunch is allowed once that terminal has ended. test: `tests/terminals/test_workspace_agent_panes.py::test_live_seat_refused_across_kinds_ended_seat_allowed`.
- 1.1.4 - Reserve then bind produces a bound pane with `owns_terminal=True`, keeps the in-flight mark until `settle`, and emits exactly one `tab.created` or `pane.added`. test: `tests/terminals/test_workspace_agent_panes.py::test_reserve_bind_emits_once`.
- 1.1.5 - With no launch terminal, or an inactive one, release removes the reserved pane and the tab it emptied, and is idempotent. test: `tests/terminals/test_workspace_agent_panes.py::test_release_is_idempotent`.
- 1.1.6 - Two concurrent reservations of the same workspace and canonical title yield exactly one reservation and one `seat_live` refusal, for same-kind and cross-kind pairs. test: `tests/terminals/test_workspace_agent_panes.py::test_concurrent_same_seat_reserves_once`.
- 1.1.7 - A `right` split is stored with axis `horizontal`, and a `down` split with axis `vertical`. test: `tests/terminals/test_workspace_agent_panes.py::test_split_axis_maps_to_storage_axis`.
- 1.1.8 - A tab reservation stores the `worktree_id` passed to `reserve`: none for isolation none, the reused id for a reused worktree, and the new id for a fresh worktree. test: `tests/terminals/test_workspace_agent_panes.py::test_reserve_stores_final_worktree_association`.
- 1.1.9 - A `create_tab` or `add_pane` failure, and a `rename_pane` failure after `add_pane` committed, each leave no pane row, tab row, in-flight mark or seat entry, and a later reserve of the same seat succeeds. test: `tests/terminals/test_workspace_agent_panes.py::test_reserve_insert_failure_leaves_nothing`.
- 1.1.10 - A reserve cancelled while its insert is running waits for the insert to settle, removes any committed row, clears the mark and the seat entry, and re-raises `CancelledError`. A later reserve of the same seat succeeds. test: `tests/terminals/test_workspace_agent_panes.py::test_reserve_cancelled_before_return_leaves_nothing`.
- 1.1.11 - Release kills an owned terminal that is still `pending` or `live` and then removes the pane, kills nothing when that terminal is already inactive, and when the kill fails, or the terminal is already `orphaned`, keeps the pane bound to that terminal, `orphaned` after a failed kill of a `live` row and still `pending` after a failed kill of a `pending` row (1.6), so a same-seat preflight is `seat_live`. After that terminal settles `exited`, `sweep_dead_panes` removes the pane and the seat is free. test: `tests/terminals/test_workspace_agent_panes.py::test_release_kills_only_an_active_owned_terminal`.
- 1.1.12 - With an active owned terminal, release still kills it and clears the mark and the seat entry when the kill raises, when `remove_pane` raises, when the removal publish raises, and when the row is already gone. Each failure is logged with the phase, the pane and terminal ids and the exception type name, a synthetic secret in the exception message is absent from the log, release does not raise, and after a successful cleanup has marked the terminal inactive release issues no kill. test: `tests/terminals/test_workspace_agent_panes.py::test_release_steps_are_independent`.
- 1.1.13 - When the rollback's `remove_pane` fails, reserve logs that failure with the pane id and the exception type name and without the exception message (a synthetic secret marker is absent from the log), clears the mark and the seat entry, and raises the original error. The residual unbound row is not a live seat, a same-seat reserve succeeds, and the next `sweep_dead_panes` removes the row. test: `tests/terminals/test_workspace_agent_panes.py::test_reserve_rollback_failure_leaves_sweepable_residue`.
- 1.1.14 - `settle` clears the mark and the seat entry after a successful reply, and until then a guarded close, move or swap of the bound pane is refused `busy`. test: `tests/terminals/test_workspace_agent_panes.py::test_mark_held_until_settle`.
- 1.1.15 - A split whose beside pane, or that pane's tab, moved to another tab, workspace or project after preflight is refused `not_found` at reserve and inserts nothing. test: `tests/terminals/test_workspace_agent_panes.py::test_split_reserve_refuses_moved_target`.

### 1.2 Executor binds a placed terminal before exec [category: code] (depends: 1.6, 1.9)
`kind: deliverable`

Targets:
- `src/gobby/agents/spawn_models.py::SpawnRequest`
- `src/gobby/agents/spawn_models.py::SpawnResult`
- `src/gobby/agents/spawn_executor.py::_runtime_spawn`
- `src/gobby/agents/spawn_executor.py::_promote_prepared`
- `src/gobby/agents/spawn_executor.py::_cleanup_timed_out_prepare`
- `src/gobby/agents/spawn_executor.py::_schedule_timeout_cleanup`
- `src/gobby/agents/spawn_executor.py::reap_stale_pending_terminals`
- `src/gobby/agents/lifecycle_reconciliation.py::LifecycleReconciliation.reap_stale_pending`
- `tests/agents/test_lifecycle_reconciliation.py::*` — scope-reason: the stale-pending case gives its fake runtime a presence answer, since the reaper now settles only on proven absence
- `src/gobby/agents/spawn_executor_runtime.py`
- `tests/agents/test_spawn_executor_placement_bind.py`

Add one optional field, `SpawnRequest.placement_binder: Callable[[str],
Awaitable[None]] | None = None`. 1.4 sets it to the reserver's `bind` for the reserved
pane, and every other caller leaves it `None`. When the binder is set, `_runtime_spawn`
awaits it with the terminal id immediately after `manager.create_pending` returns, and
before `reserve_observer`/`prepare_spawn`. The provider command was already
SRT-wrapped before `create_pending`, so the order is the decision 3 invariant:
`prepare_sandbox_launch`/`--preflight`, then `wrap_provider_command`, then
`create_pending`, then bind, then `reserve_observer`/`prepare_spawn`, then provider
exec. A binder failure fails the pending terminal through the existing
`_settle_native_spawn_failure` path and returns a failed `SpawnResult`, so the
provider never executes. That covers `busy`, `not_found` and any other exception,
including a publish failure after the binding persisted, because
`classify_native_spawn_failure` settles an exception it does not recognize as
`fail_pending`. Without a binder the order is unchanged.

Placed timeout and cancellation. As-is, a `timeout_seconds` expiry schedules
`_schedule_timeout_cleanup` and returns at once, leaving the row `pending` until a
late settlement. A `CancelledError` awaits `asyncio.shield(prepare_task)` once,
catches only `Exception`, and returns `cancelled` without consuming the prepared
result, so a second cancellation abandons a prepare that may still create the
session. The late cleanup kills through `kill_spawn_key`, which swallows a tmux
terminate error and ignores a native epoch mismatch.

With a binder, ownership starts before the row is written. `_runtime_spawn`
claims the terminal id in the 1.9 in-doubt registry before it dispatches
`create_pending` (the id comes from `mint_terminal_id`) or, on a retry, the
generation bump (the id is `existing.id`). If the id is already held, it returns
a failed `terminal_in_doubt` result and touches no row. Every reaper and reconcile path honors the
claim: `reap_stale_pending_terminals` skips a held id here, and 1.9 covers the
rest, so a slow prepare, including `timeout_seconds=None` or one longer
than `spawn_in_doubt_seconds`, is never reaped or declared absent while it can
still create its session.

One ownership boundary covers every exit after the claim. `_runtime_spawn` wraps
the rest of its body in one `try`/`except BaseException`/`finally`, and a local
flag records whether the claim was released inline or handed to the owner, so
exactly one of the two happens on every return and raise. The partition: before
`prepare_spawn` is dispatched nothing can exist on the host, so the row is
settled inline and the claim released. The exceptions are a raised
`create_pending` or bump and a raised inline settlement write. Their row
state is indeterminate, so they go to the owner. Once it is dispatched, every outcome
other than a completed promotion goes to the owner, because a prepare failure
is never proof (1.9, I1).

Every step after the claim that can outlive a cancellation is dispatched as a
task and awaited through `asyncio.shield`. That covers `create_pending` or the
bump, the bind, `reserve_observer`, the prepare, the promotion and every inline
settlement write. A caller cancellation therefore cancels only the wait, and the
task and its `to_thread` worker keep running. The handoff hands the owner that
live task, so the owner awaits the worker's real completion. The handoff itself
is synchronous (create the owner task, retain it, set the flag) with no await, so
a repeated cancellation cannot interrupt it, and it finds the flag set
afterwards.

The exits, each a case of 1.2.9:
- E1. `create_pending` or the retry bump raises. A raise does not prove that
  nothing committed: `create_pending` builds its `Terminal` after
  `db.fetchone` has committed (`terminals.py:359`), `retry_attempt_unsettled`
  does the same (`terminal_settlement.py:376`), `PostgresHubDatabase.fetchone`
  leaves its transaction before it returns (`hub/postgres.py:353`), and a lost
  commit acknowledgment is indeterminate. The owner (stage `create`) takes the
  finished task and recovers the row as below.
- E2. Cancellation during `create_pending` or the bump: the owner (stage
  `create`) awaits the task and settles a row that commits late with
  `fail_pending_attempt`.
- E3. `cancel_event` is set: `fail_pending`, and the claim is released inline.
- E4. The binder raises: `_settle_native_spawn_failure` (native) or
  `fail_pending_attempt` (tmux), and the claim is released inline.
- E5. Cancellation during the bind: the owner (stage `bind`) awaits the bind,
  then `fail_pending_attempt`.
- E6. `native_reserve_unavailable`: `fail_pending`, and the claim is released
  inline.
- E7. `reserve_observer` raises: `_settle_native_spawn_failure`, and the claim
  is released inline.
- E8. Cancellation during `reserve_observer`: the owner (stage `reserve`) awaits
  it, then `fail_pending_attempt`.
- E9. The prepare hits `TimeoutError` or `CancelledError`, done or not: the owner
  (stage `prepare`).
- E10. The prepare raises `TerminalSpawnFailed` or any other exception: the
  owner (stage `prepare`).
- E11. `_promote_prepared` returns a promoted result: the claim is released after
  that settlement.
- E12. `_promote_prepared` returns a failed result: an observer bind failure,
  `CommitSpawnRefusedError`, a native commit error, or a lost CAS. The owner
  gets stage `promote` with the resolved promotion.
- E13. `_promote_prepared` raises or is cancelled: the owner (stage `promote`)
  awaits the promotion task.
- E14. The id is already held at claim time: a failed `terminal_in_doubt` result,
  and no row is touched.

An inline settlement that is itself cancelled hands its still-running write task
to the owner the same way, and one that raises hands the finished task over for
the same read-back recovery. For a placed spawn, a prepare that raises no longer
settles the row inline: it stays `pending` under the claim, then goes `exited`
once the owner proves absence, and `_runtime_spawn` returns the same failed
result code as today.

Placed promotion defers every failure to the owner. `_promote_prepared` gains
keyword `defer_failure: bool = False`, and the placed path passes true. With
it, the observer bind failure, `CommitSpawnRefusedError`, native commit error,
commit cancellation and lost-CAS branches call neither `kill_spawn_key` nor
`_settle_native_spawn_failure` nor `fail_pending`. They return, or re-raise,
their failure with the row left as they found it, so no row is settled
`exited` before the owner's proof. `_runtime_spawn` already holds `prepared`
(locator, host epoch, process) and hands it to the owner, so the identity
survives a promotion that returns only a `SpawnResult`. Without the keyword the
branches are unchanged.

Handoff: the owner is a task created at that moment, retained in
`_TIMEOUT_CLEANUP_TASKS`, and never a done-callback. `_schedule_timeout_cleanup`
gains keyword `in_doubt_owner: bool = False`, a `stage` (`create`, `bind`,
`reserve`, `prepare` or `promote`), the live task for that stage, and `prepared`
when there is one. The placed path passes true, and the unplaced paths keep
today's done-callback. `_runtime_spawn` awaits nothing after the handoff: a
timeout returns the failed `spawn_timeout` result, bounded by
`timeout_seconds`, a prepare or promotion error returns its failed result, and a
cancellation returns `cancelled` as today.

The owner, in `spawn_executor_runtime.py`, catches every outcome of the task it
was handed, so nothing it owns is abandoned. The order depends on the stage:
- `create`, `bind` and `reserve`: no prepare was dispatched, so no host resource
  can exist. The owner awaits the task first; it never infers absence from a
  write that is still running. It then settles a committed row with
  `fail_pending_attempt`, calls `release`, and runs the deferred steps it
  returns once.
  When the drained create or bump task raised, the owner recovers the actual
  row. Under `settle_lock` it reads the row by id. It never re-executes the
  create or the bump:
  - create (minted id): no row means the insert rolled back, and that is the
    settlement. A `pending` row with this `spawn_key` is this attempt's commit:
    the owner adopts its `attempt_generation` and `attempt_started_at` and
    settles it with `fail_pending_attempt`.
  - bump (existing id): the owner captured the pre-bump pair from `existing`.
    A row that still carries that pair means the bump rolled back. The
    pre-existing attempt is not this owner's, so it writes nothing and settles
    with no write. A `pending` row with a newer pair is this attempt's bump,
    because the held claim admits no other writer (E14). The owner adopts the
    pair and settles it with `fail_pending_attempt`.
    Every failed `SpawnResult` of a retry carries the pre-bump pair as the new
    field `prior_attempt: tuple[int, datetime] | None` (`None` for a create).
    A created-isolation step that arrives after the release uses it to
    recognize a rolled-back bump (1.6).
  - A read that raises goes to the settlement retry below. The retry repeats
    only the read and the settlement it decides, on the same cadence.
  The create or bump task is drained before the read, so no write is still
  in flight and the read is final. Release and compensation then run as for a
  successful create.
- `prepare`: the row stays `pending` under the held claim while the prepare may
  still be unresolved. A `pending` row holds the seat, the storage guard refuses
  its pane, and every reaper skips the held id, so nothing needs the row
  `orphaned` before the owner waits. The owner drains the prepare (step 2) and
  continues with steps 3-6.
- `promote`: the owner awaits the promotion task first, then rereads the row under
  `settle_lock` and runs steps 1 and 3-6 with the `prepared` it was handed.

The steps:
1. at stage `promote`, moves a row the promotion left `live` to `orphaned` with
   `mark_kill_failed` (1.6), so the pane and the seat are kept (decision 5). A
   `pending` row stays `pending`: the schema admits no `orphaned` row without a
   locator, or a native one without a host epoch
   (`terminals_locator_present_when_attachable`,
   `terminals_native_attachable_has_epoch`), and a `pending` row may carry
   neither (`terminals_pending_has_no_identity`). If the CAS finds the row in
   any other state, the owner logs that state and still makes the kill
   decision from the prepared identity;
2. consumes the prepare outcome, including one that completed as the
   cancellation arrived;
3. on a prepare failure, proves absence. A failure is never proof by itself:
   tmux can create the session and then fail its dimension query, and a native
   spawn response can be lost after the host created the terminal.
   - For tmux, absence is proven when `session_present` for the spawn key's
     session is false. A present session gets the strict kill in step 4.
   - For native, absence is proven when `find_host_terminal` (1.9) returns
     `None`. A match gets the strict kill in step 4 with the returned host id and
     the client's epoch, and a raise is unproven.
4. on a prepare success, or a present session found in step 3, kills with proof.
   It builds a `Terminal` from the current row with the prepared locator, host
   epoch and process, and calls the runtime `terminate`.
   - For tmux, the kill is proven when `backend_session_present` then returns
     false.
   - For native, the kill is proven when `terminate` returns. For a current-epoch
     row the prepared epoch must also equal the client's epoch both before and
     after the call, because the host's `kill` raises on a failure. For a stale
     or missing epoch, `terminate` takes the strict stale branch (1.9), which
     returns only on a proven kill or a proven absence.
   - Any exception, including `HostUnavailableError`, is unproven.
   `is_live` answers false for an unreachable host, so it is never native proof;
5. on proof, settles `exited` (`fail_pending_attempt` for a `pending` row,
   `mark_exited_attempt` (1.9) for a `live` or `orphaned` one) and runs the
   claim's deferred compensation once (1.9). An unproven kill with the prepared
   identity in hand keeps the row as an orphan with that identity: a `pending`
   row moves to `orphaned` with the locator, locator key, host epoch and
   process in one `mark_kill_failed` CAS (1.6), and an `orphaned` row gets them
   through `record_orphan_identity` (1.9). The owner drops the deferred
   compensation so created isolation is kept, and the row stays listed for
   `terminal_kill`. A prepare failure whose absence is unproven leaves the owner
   with no identity it can record, so it enters the absence retry below;
6. releases the claim only after its final settlement write is confirmed.

Absence retry. When the prepare failed and step 3 could not prove absence (the
tmux `session_present` or the native `find_host_terminal` raised), or found
the session and the step 4 kill was unproven, the owner has no identity the
schema accepts on an `orphaned` row. It keeps the row `pending` and the claim
held, and on the settlement retry's cadence re-runs the presence check of
step 3. A check that proves absence settles `fail_pending_attempt`, releases
and runs compensation once. A check that finds the session present re-issues
the step 4 strict kill once in that cycle: tmux by the spawn key's session,
native through the host id `find_host_terminal` returned, under the current
epoch. A proven kill settles the same way. An unproven kill, or a check that
raises, schedules the next cycle. The kill was never proven, so re-issuing it
repeats no effect. A cycle never re-dispatches the prepare or runs provider
code. A session that stays present keeps the claim and the pane, with the
WARNING cadence below, because its process may still run; the daemon never
needs a restart to leave this state once the kill or the host recovers.

Storage failures are contained. A failed write never skips consuming the handed
task or the kill decision. Each write (`mark_kill_failed`,
`fail_pending_attempt`, `record_orphan_identity`, the `exited` mark) is first
retried three times with backoff. What each failure leaves:
- `mark_kill_failed` fails: the row stays `pending` or `live`, and the owner goes
  on to drain and decide. The claim still keeps every other path off the row.
- `fail_pending_attempt`, `record_orphan_identity` or the `exited` mark fails:
  that settlement did not happen. The owner logs at WARNING with the terminal id
  and the exception type name, keeps the claim held, and enters its settlement
  retry.

Settlement retry. The owner does not stop after the three immediate retries. It
stays alive, still holding the claim, and settles once storage recovers, with
no daemon restart:
- It keeps what it already holds in its own frame: the terminal id, the
  attempt generation and `attempt_started_at` captured from the committed
  row, the stage, and the one settlement it decided. That settlement is
  `exited` after a proven kill or proven absence, `fail_pending_attempt` for
  stages `create` to `reserve`, or a kept orphan with the locator, locator key,
  host epoch and process it recorded. The deferred
  compensation stays in the 1.9 registry under the held id. No new registry
  field, table or reconciler hook is added.
- Each cycle re-issues only that decided settlement under `settle_lock`, and
  every re-issued write carries both captured attempt values as its CAS
  predicate: `fail_pending_attempt` already does, `mark_exited_attempt`
  replaces the unguarded `mark_exited`, and `mark_kill_failed` and
  `record_orphan_identity` take the pair (1.6, 1.9). The read-back confirms
  only a row that still carries the same pair. For a kept orphan the cycle
  re-issues the one-CAS `mark_kill_failed` with identity while the row is
  `pending`, `mark_kill_failed` then `record_orphan_identity` while it is
  `live`, and `record_orphan_identity` once it is `orphaned`, then verifies. A
  settlement cycle never probes, kills, re-dispatches the prepare or runs
  provider code, and never turns a kept orphan into `exited`. Kill proof is
  therefore never fabricated: `exited` is re-issued only when the proof was
  obtained before the retry began or by a strict kill the absence retry proved.
- Cadence: exponential backoff from 1 s, doubling to a 60 s cap, with no
  attempt limit. Storage recovery can come at any time, so the cap bounds the
  rate, not the lifetime. The owner logs at WARNING when the retry starts and
  at most once every ten minutes after, with the terminal id, the pending
  settlement and the exception type name. It logs at INFO when the settlement
  is confirmed.
- A cycle that raises (a DB error or a `settle_lock` wait cut short), a CAS
  that matches no row, or a read-back that is not yet the required result
  schedules the next cycle. Every confirmed-settlement rule below applies
  unchanged.
- On the first confirmed settlement, the owner calls `release` once. The
  registry returns the deferred steps in that same call. The owner runs them
  for `exited` and for a confirmed `fail_pending_attempt`, where no host
  resource exists, and drops them for a kept orphan, exactly as on a
  first-try success. Because `release` removes the id and returns the steps
  atomically, compensation runs at most once however many cycles ran. A step
  deferred after the release decides from the row (1.6).

One owner per attempt. The claim is exclusive: E14 refuses a second claim on a
held id, and 1.9's `terminal_kill`, reconcile's held-row skip and both
stale-pending reapers leave a held row alone. A recovery pass that runs while
the owner is retrying therefore neither settles the row nor races the owner,
and repeated passes change nothing until the owner releases. The claim is held
for the whole retry, so the unresolved-prepare shielding, the storage guard's
pane refusal and the strict native and tmux proof are all unchanged.

Cancellation and shutdown. The owner is one task for the whole claim
lifetime after the handoff. The same task runs the stage drain, the read-back
recovery, the proof and the kill decision, the first settlement, the
settlement and absence retries, the `release` and the compensation it
returns. The handoff creates it synchronously and retains it in
`_TIMEOUT_CLEANUP_TASKS`. `_runtime_spawn` awaits nothing after the handoff,
and no other code holds or awaits the task. Any future waiter must await it
through `asyncio.shield`, so cancelling that waiter cancels only the wait.
Inside it, every offloaded write, probe and kill is its own task awaited
through `asyncio.shield`. The owner therefore keeps its frame, its claim and
its deferred steps and goes on progressing while the daemon lives. It is
never replaced, and nothing is handed off or re-run. The owner task itself is
cancelled only by event-loop teardown: `_TIMEOUT_CLEANUP_TASKS` has
no member-cancelling reader (`spawn_executor.py:837` adds and `:844`
discards), and the runner's graceful shutdown cancels only the runner's named
task attributes (`runner_lifecycle_shutdown.py::_cancel_periodic_tasks`,
`_cancel_runner_task`), with no `asyncio.all_tasks` sweep, before the loop
closes. A `CancelledError`
raised inside the owner, at any stage, is therefore the daemon stopping. It releases
nothing, runs no deferred step, logs the pending settlement at WARNING and
re-raises. The process exit empties the registry, and restart recovery (1.9)
owns the row, so a restart remains only the fallback for a daemon that stops
before storage recovers.

The claim is released only after the final settlement write succeeds and
reads back.

Success means a confirmed settlement, not merely no exception. The owner reads
the row back under `settle_lock` after each final write and releases only when
it shows the required result:
- `exited`: the row is `exited` and carries the captured `attempt_generation`
  and `attempt_started_at`. A row already `exited` with that pair counts as
  settled.
- `fail_pending_attempt`: it CASes `pending` to `exited`
  (`terminal_settlement.py:278-295`), so the row is `exited` with the captured
  or adopted pair. After a raised create or bump, a read-back that finds no row
  (create), or the row still carrying the pre-bump pair (bump), counts as
  settled with no write. A raise alone never counts.
- a kept orphan: the row is `orphaned`, carries the captured pair and the
  recorded locator, locator key, host epoch and process. A `pending` row gets
  there in one `mark_kill_failed` CAS with the identity. A `live` row takes
  `mark_kill_failed`, then `record_orphan_identity`, an `orphaned`-only CAS.

A CAS that matches no row, or a read-back in any other state, keeps the claim
held, is logged at WARNING and goes to the settlement retry. The row is never
released unrecorded to ordinary reaping.

The owner never calls `kill_spawn_key`. While the claim is held:
- `terminal_kill` and `cleanup_failed_spawn`'s terminate step (1.6) refuse
  through 1.9.
- Once 1.1 and 1.5 land, the storage guard refuses a close, move or swap of the
  pane inside the mutation transaction, and the release keeps the pane.
Nothing can therefore mark the row `exited` or free the pane before the owner
settles it, and the owner's settlement is what lets `sweep_dead_panes` prune
the pane (1.5).

Stale-pending reapers hold to I1 (1.9). As-is, both settle on a bare timeout:
`reap_stale_pending_terminals` fails a native row with no recorded host id
without asking the host and fails a tmux row after `kill_spawn_key`, which
swallows a terminate error. `LifecycleReconciliation.reap_stale_pending`, the
one the lifecycle monitor runs, calls `fail_pending` on every stale row. A
`pending` row the owner kept, or one left by a daemon that stopped mid-prepare,
could then settle while its process runs. `reap_stale_pending_terminals`
becomes the one strict reaper. It skips a held id. A tmux row settles only
when `session_present` is false after the existing kill. A native row settles
only when `find_host_terminal` (1.9) returns `None`, after the existing kill
when a host id is recorded. A present session, a listed host row or a raised
probe leaves the row `pending` for the next pass, and a listed native row for
reconcile's `pending` branches. `LifecycleReconciliation.reap_stale_pending`
delegates to it with its own `runtime_registry` (a function-level import from
the facade), which the lifecycle monitor supplies
(`lifecycle_monitor.py:186`), and returns the count; with no registry it
reaps nothing. Without a binder, the timeout and cancellation paths are
unchanged and `timeout_seconds=None` keeps today's unbounded wait. 1.4.14 and
1.7.6 run the same race through the placed spawn and placed resume boundaries.

Split `spawn_executor.py` (962 lines): move `_runtime_spawn` and `_promote_prepared`
into the new `spawn_executor_runtime.py` and re-export both names from
`spawn_executor.py`. The facade stays the patch target (memory e82d82f5), and existing
`patch("gobby.agents.spawn_executor....")` sites keep working.

The facade imports the runtime module to re-export those two names, so the runtime
module never imports the facade at module level, because that import would be
circular. Each moved function imports the helpers that stay in `spawn_executor.py`
inside its body, which is the pattern `spawn_executor_codex` already uses. Today those
helpers are `wrap_provider_command`, `derive_spawn_key`, `resolve_terminal_services`,
`kill_spawn_key`, `settle_promotion`, `_settle_native_spawn_failure`,
`_schedule_timeout_cleanup`, `_tmux_duplicate_session_error`,
`_same_live_identity` and `_persist_spawn_workspace`. Each call stays a bare name resolved from the facade at call
time. Facade patches therefore stay effective, and the source check in
`test_srt_spawn.py` still finds exactly one bare `wrap_provider_command` call in
`_runtime_spawn`. Provider functions that stay in the facade call the re-exported
`_runtime_spawn` global, and the moved `_runtime_spawn` calls the moved
`_promote_prepared` directly. No test patches `_promote_prepared`.

This deliverable adds no placement input and no caller that sets the binder, so it
lands safely on its own.

**Granularity:** fourteen acceptance items, one outcome: a placed spawn's terminal
id has exactly one owner from before the row is written until a proven
settlement.
The bind order, the exits, placed promotion and the owner are that one
lifecycle. A split would land a commit where some exit holds the claim with no
owner, or where promotion still settles rows the owner must prove. The executor
split is the size move that lets it fit.

**Research context:**
- The terminal path is in the Evidence section. `_prepare_provider_sandbox` catches
  `OSError`/`ValueError`/`SrtRuntimeError` and returns "Sandbox startup failed closed"
  before `create_pending` (decision 6, VERIFIED by R2), so a wrap failure reaches the
  executor as a failed `SpawnResult` with no terminal row.
- The binder is a plain async callable, so `spawn_models.py` does not import the
  terminals package.
- #22932 (Lane 3, gobby#14531) owns the upstream gap where
  `asyncio.to_thread(prepare_terminal_spawn)` keeps running after cancellation.
  The owner here waits on `prepare_task` whatever that path does, so this plan
  neither targets nor depends on the prepare path.
- Planned checks: `DATABASE_URL=<isolated hub> GOBBY_TEST_PROTECT=1 uv run pytest
  tests/agents/test_spawn_executor_placement_bind.py tests/agents/test_spawn_executor.py
  tests/agents/test_native_spawn.py -v`, plus ruff, and mypy on `src/`.

Consumers unchanged:
- `src/gobby/mcp_proxy/tools/spawn_agent/_request.py` — no-edit-reason: Builds SpawnRequest; the new placement_binder field defaults to None.
- `src/gobby/agents/spawn_executor_codex.py` — no-edit-reason: Imports _runtime_spawn from the facade re-export; the call is unchanged.
- `src/gobby/agents/spawn_executor_providers.py` — no-edit-reason: Reads SpawnRequest fields only; the SRT wrap is reused unchanged.
- `src/gobby/agents/spawn_executor_support.py` — no-edit-reason: The wrap_provider_command path is reused unchanged.
- `tests/agents/test_spawn_executor.py` — no-edit-reason: Patches through the spawn_executor facade, which keeps the moved names.
- `tests/agents/test_srt_spawn.py` — no-edit-reason: Reads `_runtime_spawn` source through the facade re-export, and the moved body keeps exactly one bare `wrap_provider_command` call.
- `src/gobby/agents/lifecycle_monitor.py` — no-edit-reason: `reap_stale_pending` still awaits the reconciliation method and returns its count.
- `tests/agents/test_lifecycle_monitor.py` — no-edit-reason: patches the reconciliation method and checks only the call and the count.
- `tests/agents/conftest.py` — no-edit-reason: Builds SpawnRequest without a binder; the new field defaults to None.
- `tests/agents/test_native_spawn.py` — no-edit-reason: Unplaced native spawn; the bind step is skipped without a binder.
- `tests/agents/test_spawn_executor_providers.py` — no-edit-reason: The provider wrap is unchanged.
- `tests/agents/test_verified_review_regressions.py` — no-edit-reason: Unplaced SpawnRequest construction; the field defaults to None.
- `tests/terminals/fakes.py` — no-edit-reason: Fake SpawnRequest consumers need no binder field.
- `tests/terminals/test_tmux_runtime.py` — no-edit-reason: Imports `_promote_prepared` through the facade re-export and runs the unplaced tmux path.
- `tests/agents/test_spawn_executor_droid.py` — no-edit-reason: `_droid_request` builds SpawnRequest without a binder; the new field defaults to None.
- `src/gobby/terminals/web_spawn.py` — no-edit-reason: its unplaced timeout calls `_schedule_timeout_cleanup` with the existing keywords; the placed owner is selected by `in_doubt_owner`, which defaults to false.

**Acceptance:**

- 1.2.1 - With a binder, the executor runs `wrap_provider_command`, `create_pending`, the bind, `reserve_observer`/`prepare_spawn` and provider exec in that order, and the provider argv is the SRT-wrapped command. Without a binder the order is unchanged. test: `tests/agents/test_spawn_executor_placement_bind.py::test_bind_follows_wrap_and_precedes_exec`.
- 1.2.2 - A binder failure, including a publish failure raised after `set_pane_terminal` persisted the binding, fails the pending terminal through `_settle_native_spawn_failure`, returns a failed `SpawnResult`, and never starts the provider. test: `tests/agents/test_spawn_executor_placement_bind.py::test_bind_failure_fails_pending_terminal`.
- 1.2.3 - `spawn_executor` re-exports `_runtime_spawn` and `_promote_prepared` from `spawn_executor_runtime`. symbol: `_runtime_spawn`. file: `src/gobby/agents/spawn_executor_runtime.py`.
- 1.2.4 - With a binder, a `timeout_seconds` expiry hands the held claim to the owner, the row stays `pending` while the prepare is unresolved, and `spawn_timeout` returns without awaiting the prepare. A late success with a proven kill settles the row `exited`. After a late success, a tmux kill that leaves the session present, a tmux terminate that raises, and a native stale-epoch kill whose host cannot answer and that has no process each move the row from `pending` to `orphaned` in one CAS carrying the prepared locator, locator key, epoch and process. A native stale-epoch kill with a process whose group is then dead settles `exited`. When the final settlement write succeeds, the claim is released in every case, for native and tmux. test: `tests/agents/test_spawn_executor_placement_bind.py::test_placed_timeout_holds_pending_then_late_settlement`.
- 1.2.5 - Without a binder, a timeout keeps today's pending row and late cleanup. test: `tests/agents/test_spawn_executor_placement_bind.py::test_unplaced_timeout_unchanged`.
- 1.2.6 - With a binder, repeated cancellation while the prepare is unresolved, and a cancellation that arrives as the prepare completes with either a success or a failure, each hand the prepare to exactly one owner, which consumes it, and return `cancelled`. test: `tests/agents/test_spawn_executor_placement_bind.py::test_placed_cancellation_has_one_owner`.
- 1.2.7 - The claim is taken before `create_pending` is dispatched and released after a successful promote or a bind failure. While it is held, `cleanup_failed_spawn` and `terminal_kill` each report not settled, leave the unresolved row `pending` and start no runtime terminate. A prepare slower than `spawn_in_doubt_seconds` is reaped by neither reaper. test: `tests/agents/test_spawn_executor_placement_bind.py::test_in_doubt_claim_spans_prepare`.
- 1.2.8 - A late prepare failure is not proof. A tmux session created before a failing dimension query, and a native terminal created on the host whose spawn response was lost, are each found and killed with proof before the row settles `exited`. A native probe that cannot reach the host leaves the row `pending` with the claim held, and the absence retry settles it once the host answers (1.2.12). test: `tests/agents/test_spawn_executor_placement_bind.py::test_late_prepare_failure_requires_proven_absence`.
- 1.2.9 - With a binder, each exit E1-E14 either releases the claim inline or hands it to exactly one owner, and none leaves the id held without an owner. Each case is one parametrized row. Cancellation cases cancel the caller repeatedly while the stage's worker is still running, and the owner awaits that worker's real completion. A `create_pending` whose row commits after the cancellation gets that row settled by the owner. The E12 cases (observer bind failure, `CommitSpawnRefusedError`, native commit error, lost CAS), with a kill that raises or is swallowed, leave the row unterminalized until the owner's proof, and the owner kills through the `prepared` identity. test: `tests/agents/test_spawn_executor_placement_bind.py::test_every_exit_releases_or_hands_off_the_claim`.
- 1.2.10 - An injected failure of `mark_kill_failed` still lets the owner consume the prepare and make its kill decision. A final settlement write that fails after three retries leaves the claim held and the row unsettled, and the owner enters its settlement retry. With `mark_kill_failed` failing on every attempt and the kill unproven, a `record_orphan_identity` CAS that matches no row keeps the claim held. A final `exited` or `fail_pending_attempt` read back in the wrong state also keeps the claim, and an already-`exited` row releases. On a timeout the row stays `pending` under the held claim while the prepare is still unresolved. test: `tests/agents/test_spawn_executor_placement_bind.py::test_owner_contains_storage_failures`.
- 1.2.11 - With storage failing past the three immediate retries, the owner keeps the claim and retries on the capped backoff with no restart. Once storage recovers, it settles and releases exactly once for each settlement: `exited` after a proven kill, a kept orphan (from `pending` in one `mark_kill_failed` CAS with identity, and from `live` by `mark_kill_failed` then `record_orphan_identity`), and `fail_pending_attempt` at stages `create`, `bind` and `reserve`. Every re-issued write carries the captured `attempt_generation` and `attempt_started_at`, and a read-back with another pair is not confirmation. A CAS that first matches no row and a wrong-state read-back each retry and then settle. Compensation runs once for `exited` and for `fail_pending_attempt` and is dropped for a kept orphan. For `create`, `bind` and `reserve`, a created-isolation step deferred before the release runs once after the confirmed settlement, and one deferred after the release decides from the row: `exited`, no row, or a row still carrying the result's `prior_attempt` pair removes, and any other state keeps. No settlement cycle calls terminate, the host probe, the prepare or provider code. While the owner retries, `terminal_kill`, reconcile and both stale-pending reapers leave the row unchanged, and a second claim on the id is refused. test: `tests/agents/test_spawn_executor_placement_bind.py::test_owner_retries_settlement_until_storage_recovers`.
- 1.2.12 - A prepare that fails with absence unproven, for native (`find_host_terminal` raises) and tmux (`session_present` raises, or the session stays present after an unproven kill), leaves the row `pending`, never `orphaned`, with the claim held. On the capped backoff the owner re-runs the presence check and, while the session is present, re-issues the strict kill at most once per cycle; it never re-dispatches the prepare or runs provider code. Once the check proves absence or a kill is proven, it settles `fail_pending_attempt`, releases and runs compensation once, with no restart. A prepare that succeeded followed by an unproven kill moves the `pending` row to `orphaned` with the prepared identity in one CAS. After a simulated restart such a `pending` row settles only on proof: `LifecycleReconciliation.reap_stale_pending` delegates to the strict reaper, a tmux row whose session is present and a native row the host lists or cannot answer for stay `pending`, and a native row absent from the strict listing settles `exited`. test: `tests/agents/test_spawn_executor_placement_bind.py::test_identityless_row_stays_pending_until_absence_proven`.
- 1.2.13 - Repeatedly cancelling the spawn caller and a waiter that awaits the owner through `asyncio.shield` does not stop the owner. The cancellations land during the stage drain, the first settlement, a backoff sleep and a shielded offloaded settlement write. When storage then recovers with no restart, there is one owner task, one confirmed settlement, one `release` and one compensation run. In a separate shutdown case, cancelling the owner task itself as loop teardown does, at each of those points, releases nothing, runs no deferred step, leaves the claim until process exit and leaves the row for restart recovery. test: `tests/agents/test_spawn_executor_placement_bind.py::test_owner_retry_survives_cancellation_until_shutdown`.
- 1.2.14 - A raised `create_pending` or bump is recovered by read-back and never re-executed. A create that commits and then raises, and a bump that commits and then raises, each leave a `pending` row that the owner adopts by its pair and settles `fail_pending_attempt`. A create that rolls back leaves no row, and a bump that rolls back leaves the pre-existing attempt unchanged; each settles with no write. With the read-back failing during a storage outage, the claim stays held and the owner recovers and settles once storage returns, with no restart. Compensation runs once in every case. For a rolled-back bump whose earlier attempt's row is `pending`, a created-isolation step deferred before the release is run once by the owner. One deferred after the release sees the row still carrying the result's `prior_attempt` and removes created isolation. A row carrying a newer pair keeps it, and reused isolation is never registered or removed. test: `tests/agents/test_spawn_executor_placement_bind.py::test_indeterminate_create_is_recovered_by_read_back`.

### 1.3 One daemon-scoped reserver reaches spawn_agent [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/websocket/server.py::WebSocketServer.configure_terminals`
- `src/gobby/servers/http.py::HTTPServer._init_mcp_subsystems`
- `src/gobby/mcp_proxy/registries.py::setup_internal_registries`
- `src/gobby/mcp_proxy/tools/agents_registry.py::create_agents_registry`
- `src/gobby/mcp_proxy/tools/agents_context.py::AgentsRegistryContext`
- `src/gobby/mcp_proxy/tools/agents_spawn_tools.py::register_agent_spawn_tools`
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::create_spawn_agent_registry`
- `tests/terminals/test_composition_roots.py::*` — scope-reason: add the reserver construction test beside the existing configure_terminals tests
- `tests/mcp_proxy/tools/test_agents_spawn_tools.py::*` — scope-reason: add the resolver pass-through test beside the existing spawn tool registration tests

`WebSocketServer.configure_terminals` builds one `AgentPaneReserver` next to
`WorkspaceOps`, from the same workspace manager, terminal manager, runtime registry,
`SessionManager` and `publish=self.broadcast_workspace_event`, under the same condition: a workspace
manager is present and the sessions are a `SessionManager`. Otherwise it stays `None`.
The server stores it as `agent_pane_reserver`.

The spawn tool reaches it through the existing resolver pattern of
`workspace_ops_resolver`. `HTTPServer._init_mcp_subsystems` passes
`agent_pane_reserver_resolver=lambda: getattr(services.websocket_server,
"agent_pane_reserver", None)` to `setup_internal_registries`. That passes it to
`create_agents_registry`, which stores it on `AgentsRegistryContext`, and
`register_agent_spawn_tools` passes it to `create_spawn_agent_registry`. The resolver
is called per tool call, so the registry never caches a value read before
`configure_terminals` runs, and every call gets the one instance and its lock map.

`configure_terminals` keeps its signature. Every new parameter is optional,
defaults to `None` and sits after the existing ones. The new
`AgentsRegistryContext.agent_pane_reserver_resolver` field is appended with a
`None` default, and the reserver type is imported under `TYPE_CHECKING` only.
Constructing the reserver stores references, an empty lock map and an empty seat-entry
map, and touches no
storage, so test servers that build `WorkspaceOps` also get an inert reserver.

**Granularity:** seven production Target files, but one behavior: one resolver
threaded through a fixed composition chain. Each file gains one parameter or one
attribute. Splitting the chain would leave a resolver that reaches nothing.

**Research context:**
- The chain and the `workspace_ops_resolver` precedent are in the Evidence section
  (`mcp_proxy/registries.py` passes `workspace_ops_resolver` to the workspaces
  registry; `servers/http.py` builds it from `services.websocket_server`).
- `tests/terminals/test_composition_roots.py` already drives `configure_terminals`
  (`test_configure_terminals_installs_input_activity_sink`).
- Rejected: a global singleton, a second event bus, or a reserver built per tool
  call. A per-call reserver makes the per-workspace lock ineffective, and a reserver
  without the server's publish callback bypasses the workspace event sequence gclient
  consumes.
- Planned checks: `DATABASE_URL=<isolated hub> GOBBY_TEST_PROTECT=1 uv run pytest
  tests/terminals/test_composition_roots.py tests/mcp_proxy/tools/test_agents_spawn_tools.py
  -v`, plus ruff, and mypy on `src/`.

Consumers unchanged:
- `src/gobby/runner_init/servers.py` — no-edit-reason: Calls configure_terminals with the same arguments; the reserver is built inside it from services it already receives.
- `tests/mcp_proxy/test_workspaces_registry.py` — no-edit-reason: Calls configure_terminals and setup_internal_registries with unchanged arguments; the new resolver defaults to None.
- `tests/servers/test_native_web_proxy.py` — no-edit-reason: Calls configure_terminals with an unchanged signature.
- `tests/servers/test_terminal_ws_input.py` — no-edit-reason: Calls configure_terminals with an unchanged signature.
- `tests/servers/test_terminal_ws_lease.py` — no-edit-reason: Calls configure_terminals with an unchanged signature.
- `tests/servers/test_workspace_ws.py` — no-edit-reason: Calls configure_terminals with an unchanged signature; the inert reserver it now builds changes no workspace event.
- `tests/servers/websocket/test_servers_websocket_auth.py` — no-edit-reason: Its stub configure_terminals accepts any arguments and the call shape does not change.
- `src/gobby/ai/embedding_switch_runner.py` — no-edit-reason: Calls setup_internal_registries without the new optional resolver.
- `tests/ask/test_native_probe_harness.py` — no-edit-reason: Calls setup_internal_registries without the new optional resolver.
- `tests/config/test_config_runtime_config_resolution.py` — no-edit-reason: Calls setup_internal_registries without the new optional resolver.
- `tests/dispatch/test_bundled_agent_contract.py` — no-edit-reason: Calls setup_internal_registries without the new optional resolver.
- `tests/mcp_proxy/test_merge_integration.py` — no-edit-reason: Calls setup_internal_registries without the new optional resolver.
- `tests/mcp_proxy/test_registries.py` — no-edit-reason: Calls setup_internal_registries without the new optional resolver; no registry is added or removed.
- `tests/mcp_proxy/tools/sessions/test_mcp_proxy_tools_sessions_registration.py` — no-edit-reason: Calls setup_internal_registries without the new optional resolver.
- `tests/mcp_proxy/tools/test_ask.py` — no-edit-reason: Calls setup_internal_registries without the new optional resolver.
- `tests/mcp_proxy/tools/test_review_learning.py` — no-edit-reason: Calls setup_internal_registries without the new optional resolver.
- `tests/skills/reference_library_helpers.py` — no-edit-reason: Calls setup_internal_registries without the new optional resolver.
- `tests/test_wiki_retirement_contract.py` — no-edit-reason: Calls setup_internal_registries without the new optional resolver and asserts only registry names.
- `src/gobby/mcp_proxy/tools/agents.py` — no-edit-reason: Re-exports create_agents_registry by name only.
- `tests/events/test_coordination_waits.py` — no-edit-reason: Calls create_agents_registry without the new optional resolver.
- `tests/runner_init/test_detection_registry_composition.py` — no-edit-reason: Its registrar and spawn factory fakes accept the context and keyword arguments, so the added resolver passes through.
- `src/gobby/mcp_proxy/tools/agents_checkpoint_tools.py` — no-edit-reason: Reads existing AgentsRegistryContext fields only.
- `src/gobby/mcp_proxy/tools/agents_lifecycle_tools.py` — no-edit-reason: Reads existing AgentsRegistryContext fields only.
- `src/gobby/mcp_proxy/tools/agents_query_tools.py` — no-edit-reason: Reads existing AgentsRegistryContext fields only.
- `src/gobby/mcp_proxy/tools/coordination.py` — no-edit-reason: Reads existing AgentsRegistryContext fields only.
- `tests/agents/test_terminal_timeout_checkpoint.py` — no-edit-reason: Constructs AgentsRegistryContext without the appended field, which defaults to None.
- `tests/mcp_proxy/tools/test_agent_worktree_checkpoint.py` — no-edit-reason: Constructs AgentsRegistryContext without the appended field, which defaults to None.
- `tests/mcp_proxy/tools/tasks/test_lifecycle_close_orchestration.py` — no-edit-reason: Builds the spawn registry from a runner alone to validate close-reviewer launch arguments; the resolver defaults to None.
- `tests/mcp_proxy/tools/test_parallel_dispatch.py` — no-edit-reason: Builds the spawn registry without the new optional resolver.
- `tests/skills/test_reference_library.py` — no-edit-reason: Builds the spawn registry from a runner alone to validate launch arguments; the resolver defaults to None.

**Acceptance:**

- 1.3.1 - `configure_terminals` builds exactly one `AgentPaneReserver` with the same workspace manager, terminal manager, runtime registry, sessions and publish callback as `WorkspaceOps`, and builds none when `WorkspaceOps` is not built. test: `tests/terminals/test_composition_roots.py::test_configure_terminals_builds_one_agent_pane_reserver`.
- 1.3.2 - `register_agent_spawn_tools` hands the context's resolver to `create_spawn_agent_registry` unchanged, and each resolution returns the server's single reserver. test: `tests/mcp_proxy/tools/test_agents_spawn_tools.py::test_spawn_registry_resolves_one_daemon_reserver`.

### 1.4 spawn_agent placement input, compensation and reply [category: code] (depends: 1.1, 1.2, 1.3, 1.6)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::create_spawn_agent_registry`
- `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py::_resolve_spawn_project_context`
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::spawn_agent_impl`
- `src/gobby/mcp_proxy/tools/spawn_agent/_placement.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_response.py::build_spawn_response`
- `tests/mcp_proxy/tools/spawn_agent/test_placement.py`

Add `placement: dict | None = None` to the `spawn_agent` tool in
`create_spawn_agent_registry`. Pass it to `spawn_agent_impl` with the resolved
reserver and `project_context_authoritative`. The boolean comes from
`_resolve_spawn_project_context_with_provenance`, a new sibling in `_factory.py` that
holds the body of `_resolve_spawn_project_context` and also returns whether the
context came from the explicit `project_path` or the resolved parent session.
`_resolve_spawn_project_context` becomes a two-value wrapper over it, so its other
consumers are unchanged. The spawn tool calls the new function once, in the
resolution that already selects the agent definition, so placement never re-resolves
the project.

Placement handling lives in a new module, `_placement.py`. Move the placed-launch
branch into the new `_placement.py` so that `_implementation.py` (964 lines) does not
grow past the ceiling: `spawn_agent_impl` gains only three call sites.

1. Right after the parent/`can_spawn` checks and the sandbox config resolution, and
   before isolation is created: `preflight_placement(...)`. It returns `None` when no
   placement is given. It does the following:
   - raises `sandbox_required` unless the effective sandbox config is `enabled` with
     `backend == "srt"` and `verify_srt_installation` passes;
   - raises `parent_unresolved` when `parent_session_id == system_session_id()`, or
     when `project_context_authoritative` is false;
   - raises `invalid_op` when no reserver resolves;
   - runs `AgentPaneReserver.preflight`.

   On refusal, return `{"success": false, "placement_error": code, "error": message}`
   before any side effect.
2. At dispatch, after every existing guard and `build_spawn_request`:
   `run_placed_spawn(...)` replaces the background scheduling for a placed launch. It
   calls `reserve(resolved, worktree_id=isolation_ctx.worktree_id)`, sets
   `SpawnRequest.placement_binder` to a closure that records the terminal id it
   receives and then binds the reserved pane, awaits
   `_execute_spawn_phase()` inline, and on success returns `build_spawn_response(...)`
   with the new `workspace`, `tab_ref` and `pane_ref` fields. `build_spawn_response`
   gains an optional `placement` argument that adds them. `spawn_agent_impl` passes
   its `SpawnPhase` (1.6), whose `spawn_failure` and `execute_spawn_phase` methods
   replace the old closures, to `run_placed_spawn`.
   Every reserve failure returns through `_spawn_failure`, which cleans the run, the
   child session and created isolation (decision 5). A typed refusal (`seat_live` lost
   to a concurrent launch, or `busy`) adds its `placement_error` code to that reply.
   Any other exception returns the same way. A `CancelledError` runs `_spawn_failure`
   and is re-raised, as `_execute_spawn_phase` does.
3. Duplicate placed requests. The existing task guard
   (`active_task_response_if_blocked`) runs after isolation, in `spawn_agent_impl`.
   For an unplaced request it keeps today's skipped `success: true` reply. For a
   placed request `spawn_agent_impl` passes the blocked response to
   `_placement.task_active_refusal`, one more call site, which turns it into `{"success": false, "placement_error":
   "task_active", "run_id": <active run>}` and `_spawn_failure` removes the
   isolation this call created. The precedence is decision 7: an occupied seat is
   `seat_live` at preflight; a free seat with an active task is `task_active`.

`run_placed_spawn` is the decision 5 compensation boundary. From the successful
`reserve` to the final response, it calls `settle` when the response has
`success: true`, and on every other exit it releases in a `finally`, passing the
recorded terminal id. The `finally` awaits release with the drain pattern of 1.6,
so a cancellation arriving during release cannot strand the mark or the seat.

The closure records that id before it awaits `bind`. Bind persists the binding and
then publishes, so a publish failure raises after the pane already holds the
terminal, and recording after `bind` returns would leave release no id. The id is
always the pending terminal `create_pending` made for this launch, never one read
from a pane row. The closure drops it only when bind raises `busy`: another pane
then holds that terminal, so release must not claim it, and the executor's
pending-failure path alone settles it (1.2). Release kills the recorded terminal
only when cleanup's terminate step did not reach it (decision 5). It reuses the cleanup that `finalize_executed_spawn` and
`_spawn_failure` already run, so no path cleans up twice. A placed launch defaults
`cleanup_isolation_on_failure` to true for isolation it created, so a refused or failed
seat leaves no worktree or clone. Reused `worktree_id` / `clone_id` isolation is never
removed.

**Granularity:** thirteen acceptance items and five production Target files, but one
lifecycle owner: one placed `spawn_agent` call from preflight to reply. The refusals,
the compensation boundary and the reply are the exits of that one call. Splitting
them would land a reservation without its compensation.

**Research context:**
- The guard order and both cleanup routes are in the Evidence section. Placement
  preflight sits before isolation (decision 2). Reservation sits at dispatch, after
  every existing guard, so slot, lease and task refusals never leave a pane.
- Parent and project: decision 8. For pipeline `mcp` steps the parent is the pipeline
  child session. The workspace actor passed to preflight is `parent_session_id`.
  `system_session_id` is in `storage/sessions/_constants.py`.
- SRT: the effective config is `managed_runtime_profile.sandbox_config` or
  `apply_write_grant(agent_sandbox_config(daemon_config), write_grant)`.
  `wrap_provider_command` is the single wrap entry, and a wrap failure returns a failed
  `SpawnResult` before any provider process starts (decision 6).
- `dispatch_batch` does not gain placement: it retires with `gobby build`.
- Rejected alternatives:
  - A separate `place_agent` tool: it duplicates the guards and cannot bind before
    exec.
  - Post-launch adoption: see decision 3.
  - Re-resolving the project inside placement: it can disagree with the resolution
    that selected the agent definition.
- Planned checks:
  - `DATABASE_URL=<isolated hub> GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/spawn_agent/test_placement.py tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py tests/mcp_proxy/tools/spawn_agent/test_factory.py tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py -v`
  - ruff, and mypy on `src/`.

Consumers unchanged:
- `src/gobby/ask/agents.py` — no-edit-reason: Unplaced caller of spawn_agent_impl; the placement default None keeps the background path.
- `src/gobby/dispatch/spawn.py` — no-edit-reason: Unplaced dispatch caller; retires with gobby build and gains no placement.
- `src/gobby/feedback/agent.py` — no-edit-reason: Unplaced caller; the placement default None keeps the background path.
- `src/gobby/scheduler/executor.py` — no-edit-reason: Cron agent jobs stay unplaced; cron runbooks reach placement through pipeline mcp steps.
- `src/gobby/servers/routes/agent_spawn.py` — no-edit-reason: The HTTP spawn route stays unplaced; placement is an MCP input reached by pipelines.
- `tests/mcp_proxy/tools/test_agents_spawn_evaluation.py` — no-edit-reason: Patches the two-value project resolver, which keeps its name and signature.
- `tests/mcp_proxy/tools/spawn_agent/test_mcp_proxy_tools_spawn_agent_runtime.py` — no-edit-reason: Calls build_spawn_response without placement.
- `tests/mcp_proxy/tools/tasks/test_lifecycle_close_orchestration.py` — no-edit-reason: Validates unplaced close-reviewer launch arguments against the spawn_agent schema; placement is optional.
- `tests/mcp_proxy/tools/test_parallel_dispatch.py` — no-edit-reason: dispatch_batch gains no placement.
- `tests/skills/test_reference_library.py` — no-edit-reason: Validates unplaced launch arguments against the spawn_agent schema; placement is optional.

**Acceptance:**

- 1.4.1 - A refused preflight (`invalid_placement`, `seat_live`, `not_found`, `forbidden`) creates no isolation, no child session, no agent run, no terminal and no pane. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_refused_placement_has_no_side_effects`.
- 1.4.2 - A placed spawn with a sandbox config that is not SRT, or not enabled, is refused with `sandbox_required` before any side effect. Leaf-local: until 1.8 lands, unplaced spawns are unaffected; 1.8 extends the refusal to every launch. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_placed_launch_requires_managed_srt`.
- 1.4.3 - A provider or SRT preparation failure that `finalize_executed_spawn` returns as `success: false` releases the pane, runs `cleanup_failed_spawn` exactly once, and never starts the provider. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_wrap_failure_refuses_and_releases_pane`.
- 1.4.4 - A later returned failure (terminal liveness or start-run) releases the pane, kills the bound terminal, and runs `cleanup_failed_spawn` exactly once. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_late_returned_failure_releases_pane_once`.
- 1.4.5 - An exception, a `CancelledError` and a bind `busy` conflict each release the pane, and release still runs when a `cleanup_failed_spawn` step fails. When cleanup's terminate step did not reach the terminal, release kills the recorded launch terminal, or marks it orphaned when the kill fails. After a `busy` conflict release kills nothing. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_exceptions_and_cancellation_release_pane`.
- 1.4.6 - A successful placed spawn returns synchronously with run_id, terminal_id, workspace, tab_ref and pane_ref, and keeps its pane bound. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_placed_spawn_reply_carries_refs`.
- 1.4.7 - A slot, lease or active-task refusal leaves no pane, and the loser of a reserve-time `seat_live` race cleans its run, child session and created isolation. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_late_refusals_leave_no_pane`.
- 1.4.8 - An explicit `project_path` is accepted. A resolved parent is accepted, including a pipeline child parented to the system or cron session. An ambient-only project and a parent that is the system session itself are refused with `parent_unresolved` before any side effect. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_parent_and_project_provenance`.
- 1.4.9 - A placed spawn that fails after creating its own worktree removes that worktree, and one that reused a `worktree_id` keeps it. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_failed_placed_spawn_cleans_created_isolation_only`.
- 1.4.10 - Two concurrent placed spawns of one seat through the registry yield one placed agent and one `seat_live`, and the winner's bind publishes `tab.created` or `pane.added` through the server's workspace broadcast before provider exec. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_concurrent_placed_spawns_share_one_reserver`.
- 1.4.11 - A `reserve` that raises, and a cancellation that arrives while `reserve` is running, each run `_spawn_failure` once, which removes the run, the child session and created isolation, and leave no pane, tab or in-flight mark. The cancellation is re-raised. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_reserve_failure_and_cancellation_clean_dispatch_state`.
- 1.4.12 - When bind's publish raises after `set_pane_terminal` persisted the binding, release still receives the launch terminal id. When the pending-failure settlement fails and `cleanup_failed_spawn`'s terminate step also fails before that terminal is inactive, release removes the pane and kills the terminal exactly once. When cleanup completes instead, release issues no kill. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_bind_publish_failure_keeps_release_kill_backstop`.
- 1.4.13 - Through the registry, a placed request for a free seat whose task already has an active run is refused `task_active` with that run id and removes the isolation it created. A placed request for the occupied seat is `seat_live`. An unplaced duplicate keeps the skipped reply, and no second agent starts. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_duplicate_placed_request_precedence`.
- 1.4.14 - A placed spawn whose `timeout_seconds` expires while the prepare is unresolved runs finalize, `cleanup_failed_spawn`, release and then a `pane_close` of the bound pane with the claim held. The row stays `pending`, the `pane_close` is refused `busy`, the pane stays bound and holds the seat, created isolation still exists, and nothing is marked `exited`. When the late prepare succeeds, the owner's proven kill settles the row `exited` and removes the created worktree, and `sweep_dead_panes` then frees the pane. On an unproven kill the worktree and the pane are kept. This holds for native and tmux. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_placed_timeout_race_keeps_pane_until_owner_settles`.

### 1.5 Workspace mutations refuse in-flight panes atomically [category: code] (depends: 1.6)
`kind: deliverable`

Targets:
- `src/gobby/storage/workspaces.py::*` — scope-reason: add the in-flight guards, `expected_panes` and the expected ids to `WorkspaceManager`, and move the module-level layout symbols (`LayoutLeaf`, `LayoutSplit`, `validate_layout`, `layout_pane_ids`, `_validate_node`, `_axis`, `_ratio`, `_leaf`, `_split`, `_map_leaves`, `_without_panes`, `_place`, `_with_ratio`) to `workspace_layout.py` behind a re-import
- `src/gobby/storage/workspace_layout.py`
- `src/gobby/terminals/workspace_ops.py::*` — scope-reason: pass the guard and `expected_panes` from `WorkspaceOps`, add `_KILLABLE_STATES`, and move `WAIT_CAPTURE_LINES`, `WAIT_CAPTURE_FAILURE_LIMIT`, `IDEMPOTENCY_KEY_PATTERN` and `_ACTIVE_STATES` with the pane I/O group to `workspace_pane_io.py`
- `src/gobby/terminals/workspace_pane_io.py`
- `tests/storage/test_workspaces.py::*` — scope-reason: add the guard, race, moved-target and sweep tests
- `tests/terminals/test_workspace_ops.py::*` — scope-reason: add the busy and orphaned-retry tests; retarget the wait-cap test's clock patches to the new module

As-is, the only in-flight guard is `WorkspaceOps._closing`, an in-memory read that
refuses a pane only while its `terminal_id` is NULL and runs before a separate
storage transaction. `close(workspace)` is a bare DELETE, and `move_pane`,
`move_tab` and `swap_panes` check nothing. A concurrent close, move or swap can
therefore act on a reserved or just-bound agent pane.

Storage (`WorkspaceManager`):
- `close`, `close_tab`, `remove_pane`, `move_pane`, `move_tab` and `swap_panes`
  gain keyword `refuse_in_flight: bool = False`. When it is true, the transaction
  locks every row it will change before it reads pane ids, then raises the new
  `WorkspaceBusyError` (a subclass of `InvalidWorkspaceOpError`) if any affected
  pane is in `_spawns_in_flight` or its bound terminal id is held in-doubt
  (`in_doubt.holds`, 1.9, which is safe from the DB worker thread). The claim is
taken while the reservation mark is still
  set (1.2) and the mark is cleared only after `_runtime_spawn` returns, so from
  reserve until the owner settles, one of the two always covers the pane. `release` and `_roll_back` keep the default, so
  they can always remove their own pane.
- One lock order everywhere: workspace rows by id, then tab rows by id, then pane
  rows. `close` with the guard locks the workspace row and then all its tab rows.
  `move_tab` already locks both workspace rows and adds the tab row. The pane
  operations lock their tab rows through `_lock_pane_tabs` after the owning
  workspace row.
- `close` and `close_tab` also take keyword `expected_panes: Mapping[str, str |
  None] | None = None`, the pane ids the caller read mapped to their bound terminal
  ids. When it is given, the transaction reads the current pane ids and bindings
  under its locks, and if they differ from `expected_panes` it raises
  `WorkspaceBusyError` and deletes nothing. A close that read its panes before a
  reservation inserted, bound and settled a new pane therefore refuses instead of
  deleting a pane it never authorized.
- `add_pane` gains `expected_workspace_id` and `expected_tab_id`. It locks the
  expected workspace row, then the beside pane's tab rows, and inside the insert
  transaction requires the beside pane's current tab to be `expected_tab_id`, that
  tab's workspace to be `expected_workspace_id`, and that tab's project to be the
  preflighted project. Otherwise it raises `WorkspaceNotFoundError` and inserts
  nothing. A tab can move between workspaces keeping its id, so both ids are
  checked. `create_tab` already locks its workspace row.
- The mark is set on the event-loop thread before the insert transaction opens,
  and the insert and a guarded mutation lock the same rows. Whichever runs second
  sees the other's commit: a mutation sees the marked pane and raises busy, and an
  insert sees its beside pane gone or moved and raises not found. An empty-workspace
  close and a new tab insert serialize on the workspace row.
- `sweep_dead_panes` no longer treats `orphaned` as dead: the predicate becomes
  `term.state NOT IN ('pending', 'live', 'orphaned')`. An `orphaned` terminal's
  process may still run, and its pane is the seat's only workspace association
  (decision 7). The pane is pruned once the terminal settles `exited`.
- Size: `storage/workspaces.py` is 973 lines and the guards push it past the
  ceiling. The pure layout functions (`LayoutLeaf`, `LayoutSplit`,
  `validate_layout`, `layout_pane_ids`, `_validate_node`, `_axis`, `_ratio`,
  `_leaf`, `_split`, `_map_leaves`, `_without_panes`, `_place`, `_with_ratio`) move
  to the new `storage/workspace_layout.py` and are re-imported by
  `storage/workspaces.py`, so `from gobby.storage.workspaces import
  layout_pane_ids` keeps working.

`WorkspaceOps`:
- `workspace_close`, `tab_close`, `tab_move`, `pane_swap`, `pane_move` and
  `pane_close` pass `refuse_in_flight=True`. `_db_guarded` maps
  `WorkspaceBusyError` to `WorkspaceOpError("busy")`.
- `workspace_close` and `tab_close` pass `expected_panes`, the exact map they
  computed `doomed` from. The terminals they kill after the delete are therefore
  exactly those of the panes the delete removed.
- `_closing` drops its NULL-terminal in-flight check, which the storage guard
  replaces, and collects doomed terminals from the new `_KILLABLE_STATES`
  (`pending`, `live`, `orphaned`), so a `pane_close` on a seat held by an
  `orphaned` terminal retries its kill. `_ACTIVE_STATES` is unchanged, because
  `pane_wait_for_output` also reads it.
- User closes keep remove-then-kill (decision 7). `_kill` marks a failed kill with
  `mark_kill_failed` (1.6).

Size: `src/gobby/terminals/workspace_ops.py` is 893 lines on 0.5.0 `070a19c3d4`,
after #22883's landed `workspace_pane_access.py` extraction, and 1.5 adds guard
lines to it. Move the pane I/O group, which 1.5 does not change, into the new
`src/gobby/terminals/workspace_pane_io.py` as `WorkspacePaneIOMixin`, and have
`WorkspaceOps` inherit it. The group is `pane_send_text`, `pane_send_keys`,
`pane_read`, `pane_wait_for_output`, `_runtime` and `_write` (lines ~472-592 and
~848-893), plus `WAIT_CAPTURE_LINES`, `WAIT_CAPTURE_FAILURE_LIMIT`,
`IDEMPOTENCY_KEY_PATTERN` and `_ACTIVE_STATES`. Once `_closing` reads
`_KILLABLE_STATES`, only `pane_wait_for_output` reads `_ACTIVE_STATES`.
- The mixin declares the attributes it reads (`_terminals`, `_registry`,
  `_coordinator`, `_sessions`, `_detection_registry`) and the
  `_pane_terminal`/`_db` methods as annotations, the same way the websocket
  `TerminalWsMixin` does.
- `_pane_terminal` stays in `WorkspaceOps`, because it needs `_enter` and
  `_pane_access`.
- Callers keep calling `ops.pane_*` unchanged.
- `test_pane_wait_for_output_caps_a_huge_timeout` patches `time.monotonic` and
  `asyncio.sleep` through `gobby.terminals.workspace_ops`; it retargets to
  `gobby.terminals.workspace_pane_io`.
- `workspace_ops.py` ends near 730 lines and the new module near 200.
- This is not #22883's pane-source extraction and does not touch
  `workspace_pane_access.py`.

**Granularity:** seven acceptance items and three production files, but one
invariant: a workspace mutation and an agent-pane insert serialize
on the same rows, and neither acts on an in-flight pane. The storage guard is inert
without the `WorkspaceOps` flag and the flag is meaningless without the guard.

**Research context:**
- Lock sites on main: `create_tab` locks the workspace row; `add_pane` locks the
  beside pane's tab rows and follows that pane's current tab; `close_tab` locks its
  tab row; `move_tab` locks both workspace rows; `close(workspace)` takes no lock.
- No storage caller bypasses `WorkspaceOps` for close, move or swap. Only `release`
  and `_roll_back` call `remove_pane` directly.
- `host_manager` marks a lost host's `live` rows `orphaned`. Those panes now stay
  listed until the terminal is killed or the pane is closed, which matches the
  terminal list.
- Planned checks: `DATABASE_URL=<isolated hub> GOBBY_TEST_PROTECT=1 uv run pytest
  tests/storage/test_workspaces.py tests/terminals/test_workspace_ops.py -v`, plus
  ruff and mypy on `src/`.

Consumers unchanged:
- `src/gobby/terminals/workspace_contract.py` — no-edit-reason: `_pane_of` and the ref helpers are reused unchanged.
- `src/gobby/app_context.py` — no-edit-reason: holds the WorkspaceManager; no call it makes changes signature.
- `src/gobby/mcp_proxy/tools/workspaces/registry.py` — no-edit-reason: calls WorkspaceOps methods whose signatures are unchanged; busy already maps to a typed error.
- `src/gobby/runner.py` — no-edit-reason: constructs WorkspaceManager unchanged.
- `src/gobby/runner_init/terminal_wiring.py` — no-edit-reason: wires WorkspaceManager and WorkspaceOps unchanged.
- `src/gobby/storage/workspace_address.py` — no-edit-reason: reads workspace rows only.
- `src/gobby/terminals/workspace_pane_access.py` — no-edit-reason: reads `db`, `get_pane_for_terminal` and `resolve_reference`, whose signatures 1.5 leaves unchanged.
- `src/gobby/servers/websocket/workspace_ws.py` — no-edit-reason: calls WorkspaceOps methods whose signatures are unchanged.
- `tests/mcp_proxy/test_workspaces_registry.py` — no-edit-reason: exercises unchanged WorkspaceOps signatures on panes that are not in flight.
- `tests/servers/test_workspace_ws.py` — no-edit-reason: exercises unchanged WorkspaceOps signatures on panes that are not in flight.
- `tests/servers/test_terminal_ws_golden.py` — no-edit-reason: golden payloads for panes that are not in flight are unchanged.
- `tests/storage/test_machines.py` — no-edit-reason: uses WorkspaceManager construction only.
- `tests/storage/test_workspace_address.py` — no-edit-reason: reads workspace rows only.

**Acceptance:**

- 1.5.1 - Each guarded mutation (`close`, `close_tab`, `remove_pane`, `move_pane`, `move_tab`, `swap_panes`) raises `WorkspaceBusyError` for an in-flight pane, bound or unbound, and for a pane whose bound terminal is held in-doubt after its mark was cleared, and changes nothing. test: `tests/storage/test_workspaces.py::test_guarded_mutations_refuse_in_flight_panes`.
- 1.5.2 - A guarded mutation racing an agent-pane insert in either order either refuses busy or runs against the committed pane; neither commits against a stale read. test: `tests/storage/test_workspaces.py::test_guard_and_insert_serialize`.
- 1.5.3 - `add_pane` with expected ids refuses a beside pane moved to another tab, a tab moved to another workspace, and a tab of another project in the same workspace, and inserts nothing. test: `tests/storage/test_workspaces.py::test_add_pane_refuses_moved_beside_target`.
- 1.5.4 - An empty-workspace close and a concurrent `create_tab` serialize: either the close wins and the insert fails not found, or the insert wins and the close refuses busy. test: `tests/storage/test_workspaces.py::test_empty_workspace_close_serializes_with_new_tab`.
- 1.5.5 - `sweep_dead_panes` keeps a pane whose terminal is `orphaned` and removes it once the terminal is `exited`. test: `tests/storage/test_workspaces.py::test_sweep_keeps_orphaned_panes`.
- 1.5.6 - `WorkspaceOps` close, move and swap return `busy` for an in-flight pane, and a `pane_close` on an `orphaned` pane retries its kill. test: `tests/terminals/test_workspace_ops.py::test_ops_refuse_in_flight_and_retry_orphaned_kill`.
- 1.5.7 - `storage/workspaces.py` is under 1,000 lines and `workspace_ops.py` under 850, and the moved names import from their new modules. file: `src/gobby/storage/workspace_layout.py`.
- 1.5.8 - The pane I/O methods live in `WorkspacePaneIOMixin`, `WorkspaceOps` inherits them with unchanged signatures, and the existing pane I/O tests pass unchanged apart from the retargeted clock patches. file: `src/gobby/terminals/workspace_pane_io.py`.
- 1.5.9 - A `workspace_close` or `tab_close` whose pane read precedes a reservation that inserts, binds and settles a pane before the close's transaction runs is refused `busy`, deletes nothing and kills nothing. test: `tests/terminals/test_workspace_ops.py::test_close_refuses_membership_drift_since_read`.

### 1.6 Spawn failure cleanup: one attempt, cancellation-safe, kill truth [category: code] (depends: 1.9)
`kind: deliverable`

Targets:
- `src/gobby/storage/terminal_settlement.py::TerminalSettlementMixin`
- `src/gobby/mcp_proxy/tools/spawn_agent/_failure_cleanup.py::*` — scope-reason: change `cleanup_failed_spawn`, `_terminate_spawn_process` and `start_run_or_cleanup`, and add `SpawnCleanupOnce`
- `src/gobby/mcp_proxy/tools/spawn_agent/_execution.py::finalize_executed_spawn`
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::spawn_agent_impl`
- `src/gobby/mcp_proxy/tools/spawn_agent/_spawn_phase.py`
- `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::*` — scope-reason: add the once, cancellation, independence and kill-truth tests
- `tests/storage/test_terminal_kill_settlement.py`

As-is, three defects hold for every spawn:
- `_terminate_spawn_process` logs a failed runtime kill and then calls `fail_pending`
  or `mark_exited` anyway, so a terminal whose process may still run is recorded as
  ended, and `cleanup_created_isolation` can delete the worktree under it.
- `finalize_executed_spawn` runs `cleanup_failed_spawn` on typed failures, and
  `_execute_spawn_phase` runs it again through `_spawn_failure` when anything around
  finalize raises or is cancelled, so cleanup can run twice.
- A cancellation during cleanup abandons the remaining steps.

Changes:
- `TerminalSettlementMixin.mark_kill_failed(terminal_id, *, attempt_generation,
  attempt_started_at, identity=None)`, a CAS predicated on the caller's attempt
  pair. It moves `live` to `orphaned`; the row already carries its identity.
  It moves `pending` to `orphaned` only when `identity` (locator, locator key,
  host epoch for native, process) is given, writing it in the same `UPDATE`.
  That is the only form the schema admits: `terminals_pending_has_no_identity`
  forbids writing the identity first, and
  `terminals_locator_present_when_attachable` and
  `terminals_native_attachable_has_epoch` forbid an `orphaned` row without it
  (`crates/gcore/assets/schema/baseline.sql`). A `pending` row without
  `identity` is left `pending` and the call returns `None`; it still holds the
  seat, and only a strict reaper or reconcile settles it (1.2, 1.9).
  `ALLOWED_EDGES` (`terminal_settlement.py:35`) gains `("pending",
  "orphaned")`; `_cas` raises on any edge outside it today. No schema change.
  `mark_orphaned` keeps its host-loss meaning (`live` only). Callers holding a
  `Terminal` pass its pair.
- `_terminate_spawn_process` returns whether the terminal settled, using the 1.9
  kill truth. A held in-doubt id is not terminated. After a terminate, a session
  that `backend_session_present` still reports counts as a failed kill. A proven
  kill settles as today (`fail_pending` for `pending`, `mark_exited` for `live`
  or `orphaned`). A failed kill or a held id calls `mark_kill_failed` and returns
  false.
- `cleanup_failed_spawn` runs its steps independently: record the spawn error,
  terminate, forget the run, terminalize the run, clean runtime state, clean
  created isolation, delete the child session. Each failed step is logged at
  WARNING with the phase, the run id, the terminal id when known and the
  exception type name, and cleanup never raises. When the
  terminate step returned false, the isolation step keeps created isolation and
  logs why. When the terminal is held in-doubt, the isolation step instead
  registers created-isolation removal with `in_doubt.defer` (1.9), and the owner
  runs it once after a proven settlement. If `defer` returns false because the
  claim has already ended, the step decides from the current row: `exited`
  removes created isolation, and so does no row for the id. A row that still
  carries the failed result's `prior_attempt` pair (1.2) also removes it: the
  bump rolled back, and this spawn's attempt never existed, even when that
  earlier attempt's row is `pending`. Each of these is final because the owner
  drained the create or bump task and read the row back before it released,
  so no row or bump for that attempt can commit later.
  `finalize_executed_spawn` passes the failed result it already holds. Any
  other row keeps created isolation. That includes a newer pair the step cannot
  attribute to this spawn, so an unresolved current attempt always errs toward
  keeping. Reused worktrees and
  clones are never registered.
- Sanitized failure logs (1.1 and 1.6). Spawn, bind, release and cleanup
  exceptions can carry prompt text, environment values or command lines, and
  #22962 (`682d7cf957`) removed that exposure from terminal cleanup. These exits
  log `type(exc).__name__` with the phase and the run, pane and terminal ids.
  They never pass `exc_info`, the exception message or a traceback. Each test
  that drives one of these failures raises an exception whose message holds a
  synthetic secret marker and asserts the type name is in the captured log and
  the marker is not, as `tests/agents/test_terminal_cleanup.py::test_terminal_cleanup_failure_logs_do_not_include_exception_text` does.
- `SpawnCleanupOnce`, a small class in `_failure_cleanup.py`, owns cleanup for one
  spawn attempt. `spawn_agent_impl` creates one per call and passes it to every
  `cleanup_failed_spawn` call site, including `finalize_executed_spawn` and
  `start_run_or_cleanup`.
- Size: `_implementation.py` is 964 lines. The `_spawn_failure`,
  `_execute_spawn_phase` and `_run_spawn_phase` closures (about 65 lines) move out
  of `spawn_agent_impl` into a `SpawnPhase` class in the new `_spawn_phase.py`,
  constructed once per call with the values the closures captured and the
  attempt's `SpawnCleanupOnce`. The file shrinks, and the new call-site keywords
  fit. The first call starts one task; every later call awaits
  that same task and starts nothing. Callers await it with the drain pattern of
  `shielded_terminal_delivery`: while the task is not done, await
  `asyncio.shield(task)`, keep the first `CancelledError`, and re-raise it after the
  task settles. It does not call `shielded_terminal_delivery` itself, because that
  returns `None` when admission is closed.
- Lifetime: the object lives exactly as long as the spawn attempt's closures, so it
  retires with the attempt and nothing accumulates. A resume successor is a new
  attempt with a new run id (`resume_executor.py` mints one). A cancellation that
  arrives after the task settled awaits the finished task, starts nothing, and is
  re-raised. Later independent recovery, meaning `terminal_kill`,
  `reap_stale_pending` and host reconciliation, acts on terminal and run rows and
  never calls `cleanup_failed_spawn`.

**Granularity:** six acceptance items across three production files, but one
behavior: a failed spawn is cleaned once and records only what its kill proved. The
single owner, the independent steps and the kill truth are one contract; each alone
leaves a double cleanup or a false `exited`.

**Research context:**
- Kill truth to copy: `termination.py::kill_terminal` raises when the runtime
  terminate fails and marks the row `exited` only after success.
  `lifecycle_reconciliation.py::reap_stale_pending` fails `pending` rows older than
  `spawn_in_doubt_seconds` (150) without a kill, so a row left `pending` after a
  failed kill would later be recorded as ended.
- `mark_exited` already accepts `orphaned`, so `terminal_kill` settles a retried
  kill with no new method.
- Planned checks: `DATABASE_URL=<isolated hub> GOBBY_TEST_PROTECT=1 uv run pytest
  tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py
  tests/mcp_proxy/tools/spawn_agent/test_execution.py
  tests/storage/test_terminal_kill_settlement.py -v`, plus ruff and mypy on `src/`.

Consumers unchanged:
- `src/gobby/terminals/host_manager.py` — no-edit-reason: host loss still uses `mark_orphaned` on `live` rows.
- `src/gobby/storage/terminals.py` — no-edit-reason: TerminalManager inherits the new CAS from the mixin.
- `tests/mcp_proxy/tools/spawn_agent/test_durable_spawn_error.py` — no-edit-reason: calls cleanup_failed_spawn without an owner; the keyword defaults to None and runs one attempt.
- `src/gobby/ask/agents.py` — no-edit-reason: spawn_agent_impl keeps its signature.
- `src/gobby/dispatch/spawn.py` — no-edit-reason: spawn_agent_impl keeps its signature.
- `src/gobby/feedback/agent.py` — no-edit-reason: spawn_agent_impl keeps its signature.
- `src/gobby/scheduler/executor.py` — no-edit-reason: spawn_agent_impl keeps its signature.
- `src/gobby/servers/routes/agent_spawn.py` — no-edit-reason: spawn_agent_impl keeps its signature.

**Acceptance:**

- 1.6.1 - `mark_kill_failed` moves a `live` row to `orphaned`, moves a `pending` row to `orphaned` only with a full identity written in the same statement (which passes the schema CHECKs), leaves a `pending` row without identity `pending`, and leaves other states, and a row with another attempt pair, unchanged. test: `tests/storage/test_terminal_kill_settlement.py::test_mark_kill_failed_cas`.
- 1.6.2 - A failed runtime kill of a native pid-less `pending` terminal, a tmux `pending` terminal whose session is still present after terminate, and a held in-doubt id each leave the row `pending` and keep created isolation, and a failed kill of a `live` terminal leaves it `orphaned` and listed; a successful kill settles it and cleanup removes created isolation. test: `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::test_failed_kill_orphans_and_keeps_isolation`.
- 1.6.3 - A failure inside `finalize_executed_spawn`'s cleanup followed by `_spawn_failure` runs cleanup once. test: `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::test_cleanup_runs_once_per_attempt`.
- 1.6.4 - A cancellation during cleanup, and a second cancellation during a later step, let every step finish once and re-raise the first cancellation; a cancellation after cleanup settled starts nothing. test: `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::test_cleanup_survives_cancellation`.
- 1.6.5 - With each step failing in turn, every later step still runs, each failure is logged with the phase, the run id and the exception type name, a synthetic secret in the exception message is absent from the log, and cleanup does not raise. test: `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::test_cleanup_steps_are_independent`.
- 1.6.7 - With the terminal held in-doubt, cleanup registers created-isolation removal with the claim and removes nothing itself, and reused isolation is not registered. After the claim ends, cleanup removes created isolation only for an `exited` row, no row, or a row still carrying the failed result's `prior_attempt` pair; a `pending`, `live` or `orphaned` row with any other pair keeps it. test: `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::test_held_terminal_defers_isolation_to_owner`.
- 1.6.6 - Every production `cleanup_failed_spawn` call passes the attempt's `SpawnCleanupOnce`. symbol: `SpawnCleanupOnce`. file: `src/gobby/mcp_proxy/tools/spawn_agent/_failure_cleanup.py`.

### 1.7 Placed resume re-places against current state [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `src/gobby/agents/resume_metadata.py::build_resume_metadata`
- `src/gobby/mcp_proxy/tools/spawn_agent/_runtime.py::*` — scope-reason: pass the validated placement into the resume snapshot
- `src/gobby/agents/resume_executor.py::resume_agent_run`
- `src/gobby/agents/resume_executor.py::_persist_resume_runtime`
- `src/gobby/agents/resume_executor.py::_rollback_prepared_resume`
- `src/gobby/agents/resume_executor.py::_park_unlaunched_successor`
- `src/gobby/agents/resume_executor.py::_fire_resume_started`
- `src/gobby/agents/resume_executor_settlement.py`
- `src/gobby/agents/resume_placement.py`
- `src/gobby/runner_lifecycle_agents.py::*` — scope-reason: pass the daemon reserver to `resume_agent_run`
- `src/gobby/runner_lifecycle_reconcile.py::*` — scope-reason: pass the daemon reserver at both resume call sites
- `src/gobby/dispatch/daemon_resume.py::*` — scope-reason: pass the daemon reserver to `resume_agent_run`
- `tests/agents/test_resume_placement.py`

As-is, `resume_agent_run` builds a `SpawnRequest` from the persisted snapshot and
awaits `_runtime_spawn` with no placement, so a resumed placed agent would launch
unplaced. On failure it parks the successor with `_park_unlaunched_successor` and
returns `ResumeAgentResult(False, ...)`.

Changes (Program Director ruling, Option A):
- The snapshot gains `placement`: the validated placement input (kind, workspace id,
  canonical title and, for `split`, the beside pane id and axis). It is input for a
  fresh preflight, never authority to skip one.
- `resume_agent_run` gains keyword `agent_pane_reserver=None`. All four callers pass
  the daemon reserver from `websocket_server` (1.3). `init_servers` runs before any
  resume loop, so the reserver exists whenever a workspace manager does.
- When the snapshot carries `placement`, the new `resume_placement.py` runs the
  placed launch:
  - no reserver: `placement_unavailable`;
  - the managed SRT check of decision 6: `sandbox_required`;
  - preflight with the persisted `parent_session_id` as actor; a parent that no
    longer resolves is `parent_unresolved`;
  - `reserve` with the snapshot's worktree id, the binder, `_runtime_spawn`, then
    `settle` on success or release on failure, awaited with the drain pattern of
    1.6.

  Every refusal and failure (`not_found` for a moved or missing target, `seat_live`
  for an occupied seat including `orphaned`, `forbidden`, a wrap failure, a bind
  conflict, a timeout) parks the successor through `_park_unlaunched_successor` and
  returns a typed error. There is no unplaced fallback. Resume creates no isolation,
  so it removes none.
- Size: `resume_executor.py` is 972 lines. `_persist_resume_runtime`,
  `_rollback_prepared_resume`, `_park_unlaunched_successor` and
  `_fire_resume_started` move to the new `resume_executor_settlement.py` and are
  re-imported, and the placed branch lives in `resume_placement.py`, so
  `resume_agent_run` gains one call.

**Granularity:** five acceptance items and seven production files, but one flow: a
resumed placed agent is re-placed or refused. The callers only pass the reserver,
and the moves exist only to make room for the call.

**Research context:**
- Resume callers: `runner_lifecycle_agents.py` (`_retry_parked_non_task_resumes`),
  `runner_lifecycle_reconcile.py` (two sites) and `dispatch/daemon_resume.py`
  (through `services`).
- The snapshot is built by `resume_metadata.py::build_resume_metadata`, called from
  `spawn_agent/_runtime.py`.
- Planned checks: `DATABASE_URL=<isolated hub> GOBBY_TEST_PROTECT=1 uv run pytest
  tests/agents/test_resume_placement.py tests/agents/test_resume_executor.py
  tests/agents/test_resume_metadata.py tests/dispatch/test_daemon_resume.py -v`,
  plus ruff and mypy on `src/`.

Consumers unchanged:
- `tests/dispatch/test_daemon_resume.py` — no-edit-reason: the new reserver keyword is optional and the fake resume accepts keywords.

**Acceptance:**

- 1.7.1 - A placed agent's resume snapshot carries its validated placement, and an unplaced agent's carries none. test: `tests/agents/test_resume_placement.py::test_snapshot_carries_validated_placement`.
- 1.7.2 - A placed resume re-runs preflight, reserve and bind before exec, and on success the pane is bound to the new terminal and settled. test: `tests/agents/test_resume_placement.py::test_placed_resume_replaces_before_exec`.
- 1.7.3 - A moved or missing target, an occupied seat (including `orphaned`), a forbidden project, a wrap failure and a missing reserver each park the successor, return a typed error, start no provider and leave no pane. test: `tests/agents/test_resume_placement.py::test_placed_resume_refusals_park_successor`.
- 1.7.4 - A placed resume failure or cancellation runs cleanup and release once each and never falls back to an unplaced launch. test: `tests/agents/test_resume_placement.py::test_placed_resume_cleanup_once`.
- 1.7.5 - `resume_executor.py` is under 1,000 lines and the moved helpers import from `resume_executor_settlement.py`. file: `src/gobby/agents/resume_executor_settlement.py`.
- 1.7.6 - A placed resume cancelled repeatedly while its prepare is unresolved hands the prepare to the 1.2 owner and keeps the pane bound to the `pending` row. The owner's late settlement then lets the pane be freed, and no unplaced fallback runs. test: `tests/agents/test_resume_placement.py::test_placed_resume_cancel_keeps_in_doubt_owner`.

### 1.8 spawn_agent and resume never launch unsandboxed [category: code] (depends: 1.4, 1.6, 1.7)
`kind: deliverable`

Targets:
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::spawn_agent_impl`
- `src/gobby/mcp_proxy/tools/spawn_agent/_sandbox_gate.py`
- `src/gobby/agents/sandbox_gate.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_placement.py`
- `src/gobby/agents/resume_executor.py::resume_agent_run`
- `tests/mcp_proxy/tools/test_spawn_agent_impl_provider.py::*` — scope-reason: the disabled-sandbox test now asserts refusal
- `tests/servers/routes/test_agent_spawn_routes.py::*` — scope-reason: the disabled-sandbox route test now asserts refusal
- `tests/mcp_proxy/tools/spawn_agent/test_sandbox_gate.py`
- `tests/agents/test_resume_sandbox_gate.py`
- `tests/conftest.py::*` — scope-reason: add the non-autouse stub_srt_verifier fixture
- `tests/mcp_proxy/tools/spawn_agent/test_factory.py::*` — scope-reason: `test_agent_sandbox_defaults_come_from_daemon_config` spawns with `enabled` false and now expects `sandbox_required`; add the stub_srt_verifier pytestmark
- `tests/mcp_proxy/tools/spawn_agent/test_agy_gate.py::*` — scope-reason: add `pytestmark = pytest.mark.usefixtures("stub_srt_verifier")`; its unit spawns reach the gate with the enabled `srt` default
- `tests/mcp_proxy/tools/spawn_agent/test_error_handling.py::*` — scope-reason: add `pytestmark = pytest.mark.usefixtures("stub_srt_verifier")`; its unit spawns reach the gate with the enabled `srt` default
- `tests/mcp_proxy/tools/spawn_agent/test_event_loop.py::*` — scope-reason: add `pytestmark = pytest.mark.usefixtures("stub_srt_verifier")`; its unit spawns reach the gate with the enabled `srt` default
- `tests/mcp_proxy/tools/spawn_agent/test_execution.py::*` — scope-reason: add `pytestmark = pytest.mark.usefixtures("stub_srt_verifier")`; its unit spawns reach the gate with the enabled `srt` default
- `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::*` — scope-reason: add `pytestmark = pytest.mark.usefixtures("stub_srt_verifier")`; its unit spawns reach the gate with the enabled `srt` default
- `tests/mcp_proxy/tools/spawn_agent/test_fallback_agent.py::*` — scope-reason: add `pytestmark = pytest.mark.usefixtures("stub_srt_verifier")`; its unit spawns reach the gate with the enabled `srt` default
- `tests/mcp_proxy/tools/spawn_agent/test_initial_variables.py::*` — scope-reason: add `pytestmark = pytest.mark.usefixtures("stub_srt_verifier")`; its unit spawns reach the gate with the enabled `srt` default
- `tests/mcp_proxy/tools/spawn_agent/test_mcp_proxy_tools_spawn_agent_dedup.py::*` — scope-reason: add `pytestmark = pytest.mark.usefixtures("stub_srt_verifier")`; its unit spawns reach the gate with the enabled `srt` default
- `tests/mcp_proxy/tools/spawn_agent/test_project_context.py::*` — scope-reason: add `pytestmark = pytest.mark.usefixtures("stub_srt_verifier")`; its unit spawns reach the gate with the enabled `srt` default
- `tests/mcp_proxy/tools/spawn_agent/test_project_scope.py::*` — scope-reason: add `pytestmark = pytest.mark.usefixtures("stub_srt_verifier")`; its unit spawns reach the gate with the enabled `srt` default
- `tests/mcp_proxy/tools/spawn_agent/test_worktree_reference_resolution.py::*` — scope-reason: add `pytestmark = pytest.mark.usefixtures("stub_srt_verifier")`; its unit spawns reach the gate with the enabled `srt` default
- `tests/mcp_proxy/tools/test_agents_spawn_tools.py::*` — scope-reason: add `pytestmark = pytest.mark.usefixtures("stub_srt_verifier")`; its unit spawns reach the gate with the enabled `srt` default
- `tests/agents/test_backend_ingress.py::*` — scope-reason: add `pytestmark = pytest.mark.usefixtures("stub_srt_verifier")`; its unit spawns reach the gate with the enabled `srt` default
- `tests/agents/test_local_context_setup.py::*` — scope-reason: add `pytestmark = pytest.mark.usefixtures("stub_srt_verifier")`; its unit spawns reach the gate with the enabled `srt` default
- `tests/tasks/test_plan_gate.py::*` — scope-reason: add `pytestmark = pytest.mark.usefixtures("stub_srt_verifier")`; its unit spawns reach the gate with the enabled `srt` default
- `tests/workflows/test_step_snapshot_semantics.py::*` — scope-reason: add `pytestmark = pytest.mark.usefixtures("stub_srt_verifier")`; its unit spawns reach the gate with the enabled `srt` default
- `tests/agents/test_resume_executor.py::*` — scope-reason: add the stub_srt_verifier pytestmark and give each resumed run an `srt` snapshot config; a case with none now expects `sandbox_required`
- `tests/ask/test_permissions.py::*` — scope-reason: its existing verifier patch also covers `gobby.agents.sandbox_gate.verify_srt_installation`, which ask runtime-profile spawns now reach

Decision 13 makes managed SRT unconditional for `spawn_agent`.

- The new `src/gobby/agents/sandbox_gate.py` holds `require_managed_srt(config) ->
  SandboxConfig` and `SandboxRequiredError`. It raises unless the config is
  `enabled` with `backend == "srt"` and `verify_srt_installation` passes. The
  effective-config resolution in `spawn_agent_impl` (the
  `managed_runtime_profile.sandbox_config` or `apply_write_grant(...)` expression)
  moves into the new `src/gobby/mcp_proxy/tools/spawn_agent/_sandbox_gate.py` as
  `resolve_spawn_sandbox`, which returns the gated config.
  `spawn_agent_impl` calls it where the expression stood, before isolation, and a
  refusal returns `{"success": false, "error_code": "sandbox_required"}` with no side
  effect. `preflight_placement` (1.4) uses the same helper, so placed and unplaced
  launches share one check.
- The sandbox resolution block of `resume_agent_run` (snapshot or runtime-profile
  config, then the `SandboxLaunch` default) moves into the new
  `src/gobby/agents/sandbox_gate.py` as `resolve_resume_sandbox`, which ends in
  `require_managed_srt`. A missing snapshot config, `enabled: false` and a
  non-`srt` backend each park the successor through `_park_unlaunched_successor`
  and return `sandbox_required`; the `SandboxLaunch(backend="provider-native",
  enforced=False)` default is removed.
- The `provider-native` backend value stays valid in config for other surfaces;
  `spawn_agent` refuses it.

**Granularity:** five acceptance items across five production files, but one
invariant: no `spawn_agent` launch or resume starts a provider outside managed SRT.
Splitting spawn from resume would leave one unsandboxed path open.

**Research context:**
- Resolution site: `spawn_agent/_implementation.py` resolves the effective config
  once; `_runtime_spawn` wraps with `wrap_provider_command`, and resume re-enters
  `_runtime_spawn`. Placed launch goes through `execute_spawn` and `_runtime_spawn`
  (1.2), so it inherits the wrap.
- `tests/conftest.py` disables `agent_sandbox` only on the CLI mock config, so CLI
  start tests never probe SRT. No spawn test patches `verify_srt_installation`
  today (checked on 0.5.0 `070a19c3d4`), and `agent_sandbox_config` defaults to
  enabled `srt`. Once the gate lands, every `spawn_agent_impl` test that expects
  success would therefore probe the real pinned install, and would fail on a
  machine without it.
- Test stub (Program Director design review, 2026-09-27). `tests/conftest.py`
  gains one named, non-autouse fixture, `stub_srt_verifier`. It patches
  `gobby.agents.sandbox_gate.verify_srt_installation` to return a stub
  installation.
- Each unit module whose spawns reach the gate opts in with
  `pytestmark = pytest.mark.usefixtures("stub_srt_verifier")`. Those modules are
  the Targets above: they call `spawn_agent_impl` or the spawn registry for
  real, or call `resume_agent_run` for real, from 0.5.0 `070a19c3d4`.
- `test_resume_executor.py` also gives its resumed runs an `srt` snapshot
  config.
- `test_permissions.py` extends its existing verifier patch to the gate's
  import.
- `test_factory.py`'s daemon-config case spawns with `enabled` false and now
  expects `sandbox_required`.
- There is no collection hook and no path allowlist.
- Two separate kinds of verifier coverage:
  - (a) Gate unit cases. `test_sandbox_gate.py` and `test_resume_sandbox_gate.py`
    do not use the fixture; each case patches the gate's verifier explicitly to
    pass or to raise, and asserts the outcome.
  - (b) Real verifier cases, with no stub. `tests/agents/test_srt_runtime.py`
    already checks the verifier against an isolated `GOBBY_HOME`. One new gate
    case,
    `test_sandbox_gate.py::test_gate_refuses_when_isolated_srt_is_missing`,
    runs with no patch and `GOBBY_HOME` set to an empty `tmp_path`, so the real
    verifier fails and the spawn is refused `sandbox_required`.
- Not stubbed:
  - Modules that replace `spawn_agent_impl` or `resume_agent_run` with fakes
    never reach the gate and stay unchanged. Those are
    `tests/dispatch/test_dispatcher.py`, `tests/dispatch/test_daemon_resume.py`,
    `tests/dispatch/test_spawn_forwarding.py`,
    `tests/build/test_dispatcher_stage_wake.py`,
    `tests/build_pipeline/test_build_pipeline_service.py`,
    `tests/feedback/test_feedback_agent.py`,
    `tests/storage/test_stage_review_findings.py` and
    `tests/scheduler/test_cron_executor.py`.
  - e2e and integration suites are never stubbed.
  - V1 runs all of these. If one fails on the gate, the fix is an explicit
    per-case patch in a unit module, or isolated real SRT in e2e and
    integration; never a blanket stub.
- `tests/dispatch/test_daemon_resume.py` replaces `resume_agent_run` with a fake,
  so the resume gate never runs there.
- 1.8.5 needs no deferral. #22961 landed at `08e54acb95`, which enforces the
  agent MCP workflow on unmarked REST and CLI calls in
  `servers/routes/mcp/endpoints/execution.py`, and at `2c2b569fe3`, which makes
  config writes operator-only. It has been live since the 2026-09-27 restart.
- Size: the sandbox resolution in
  `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py` (964 lines) is a
  split: it and its new checks move to
  `src/gobby/mcp_proxy/tools/spawn_agent/_sandbox_gate.py`, so the file shrinks.
- Size: the sandbox resolution block in `src/gobby/agents/resume_executor.py`
  (972 lines) is a split: it and its new checks move to
  `src/gobby/agents/sandbox_gate.py`, so the file shrinks.
- Planned checks: `DATABASE_URL=<isolated hub> GOBBY_TEST_PROTECT=1 uv run pytest
  tests/mcp_proxy/tools/spawn_agent/test_sandbox_gate.py
  tests/agents/test_resume_sandbox_gate.py
  tests/mcp_proxy/tools/test_spawn_agent_impl_provider.py
  tests/servers/routes/test_agent_spawn_routes.py tests/agents/test_srt_spawn.py -v`,
  plus ruff and mypy on `src/`.

Consumers unchanged:
- `src/gobby/agents/sandbox.py` — no-edit-reason: `agent_sandbox_config` still reads the config store; the gate refuses a disabled result.
- `src/gobby/ask/agents.py` — no-edit-reason: its managed runtime profile config passes through the same gate.
- `src/gobby/scheduler/executor.py` — no-edit-reason: cron spawns pass through the same gate.
- `src/gobby/feedback/agent.py` — no-edit-reason: passes through the same gate.
- `src/gobby/dispatch/spawn.py` — no-edit-reason: passes through the same gate.
- `tests/agents/test_srt_spawn.py` — no-edit-reason: the wrap call site is unchanged.
- `src/gobby/servers/routes/agent_spawn.py` — no-edit-reason: it returns `spawn_agent_impl`'s error payload unchanged, so a refusal surfaces as `sandbox_required`; its route test is a Target.
- `tests/dispatch/test_daemon_resume.py` — no-edit-reason: it replaces `resume_agent_run` with a fake, so the resume gate never runs.

**Acceptance:**

- 1.8.1 - An unplaced spawn whose effective config is `enabled: false`, or whose backend is `provider-native`, is refused `sandbox_required` before isolation, a child session or a run exists. test: `tests/mcp_proxy/tools/spawn_agent/test_sandbox_gate.py::test_unsandboxed_config_refused_before_side_effects`.
- 1.8.2 - A managed runtime profile config goes through the same gate. test: `tests/mcp_proxy/tools/spawn_agent/test_sandbox_gate.py::test_runtime_profile_config_is_gated`.
- 1.8.3 - A resume with no snapshot config, `enabled: false` or a non-`srt` backend parks the successor, returns `sandbox_required` and starts no provider. test: `tests/agents/test_resume_sandbox_gate.py::test_resume_refuses_unsandboxed_config`.
- 1.8.5 - From a spawned agent, a REST or CLI MCP call is rule-enforced. test: `tests/mcp_proxy/tools/spawn_agent/test_sandbox_gate.py::test_loopback_mcp_calls_are_rule_enforced`.
- 1.8.6 - With no verifier patch and `GOBBY_HOME` set to an empty directory, the real `verify_srt_installation` fails and the gate refuses the spawn `sandbox_required`; gate unit cases that patch the verifier assert both outcomes explicitly. test: `tests/mcp_proxy/tools/spawn_agent/test_sandbox_gate.py::test_gate_refuses_when_isolated_srt_is_missing`.

### 1.9 In-doubt spawn ownership, kill truth and restart recovery [category: code]
`kind: deliverable`

Targets:
- `src/gobby/terminals/in_doubt.py`
- `src/gobby/terminals/termination.py::*` — scope-reason: `kill_terminal` refusal, lock and proof, and `TerminalStore` gains `settle_lock`
- `src/gobby/storage/terminal_settlement.py::TerminalSettlementMixin`
- `src/gobby/terminals/native_runtime.py::NativeTerminalRuntime`
- `src/gobby/terminals/native_host_probe.py`
- `src/gobby/terminals/host_reconcile.py::reconcile_host_inventory`
- `src/gobby/terminals/host_reconcile.py::SupportsIdentityLookup`
- `src/gobby/agents/lifecycle_reconciliation.py::LifecycleReconciliation`
- `tests/terminals/test_in_doubt_kill_truth.py`
- `tests/terminals/test_host_reconcile_orphans.py`
- `tests/terminals/test_termination.py::*` — scope-reason: the existing kill cases give their fake store a settle lock and their fake runtime a presence answer
- `tests/terminals/test_native_runtime.py::*` — scope-reason: the #22530 terminate case gives its fake host a listing, and the listed, absent and unreachable stale cases are added

As-is, no kill, reaper or reconcile path knows that a prepare it cannot see may
still create the session, and a runtime kill can report success without proving
it. `TmuxTerminalRuntime.terminate` calls `kill_session` and ignores its `False`
return, and `kill_terminal` then marks the row `exited`. After a restart,
`reconcile_host_inventory` has no branch for a host row whose durable row is
`orphaned`, so an `orphaned` row whose recorded identity is stale keeps it, and
`terminal_kill` on a row with a missing or stale epoch settles it `exited`
through the #22530 no-op while its PTY may still run. The schema admits no
`orphaned` row without identity, so the 1.2 owner keeps an identity-less row
`pending` (1.6).

Invariants:
- I1. A row becomes `exited` only after one of:
  - a proven kill;
  - a proven absence: a tmux session that is not present, a strict native host
    listing without it, or reconcile's host listing without it once the in-doubt
    window has passed and no owner holds it;
  - a recorded process group verified dead.
  A prepare failure alone is never proof.
- I2. An `orphaned` row that the current host lists is killed only through its
  current host identity. Reconcile records that identity and any process the
  host reports, and `kill_terminal` resolves it through the strict probe when
  reconcile has not run yet.
- I3. The #22530 contract is unchanged for a row with a recorded stale epoch that
  the current host's strict listing no longer contains: reap the recorded
  process, then `exited`.

Changes:
- `in_doubt.py` holds a process-level registry of claimed terminal ids. It guards
  its map with one `threading.Lock`, and every call is synchronous, short and
  never awaits. `claim`, `defer` and `release` run on the event loop. `holds` is
  also called from the DB worker thread inside the 1.5 storage guard's
  transaction, as `_spawns_in_flight` already is. The registry lock is a leaf
  lock: it is held only inside a registry call and never while a DB row lock or
  `settle_lock` is acquired, so it cannot join a lock cycle. The calls:
  - `claim(terminal_id)`, `holds(terminal_id)`;
  - `defer(terminal_id, step) -> bool`, which returns false when the id is not
    held;
  - `release(terminal_id) -> list[step]`, which removes the id and returns its
    deferred steps in one call under the registry lock, before the owner awaits
    anything. A `defer` after that returns false and decides from the row (1.6).
    The owner runs each returned step independently: a failed step is logged at
    WARNING and the rest still run.
  The registry lives in the terminals package so the storage guard, termination,
  reconcile and the spawn executor can all import it. It is empty after a restart,
  which means no owner survived.
- `kill_terminal` takes `terminals.settle_lock(terminal.id)` and rereads the row
  under it, so a stale `Terminal` argument never decides the outcome.
  `TerminalStore` gains `settle_lock`, which `TerminalManager` already has. Under
  the lock:
  - a held id raises the new `TerminalInDoubtError` before any terminate;
  - otherwise it terminates, then checks
    `agents/capture.py::backend_session_present` (imported inside the function
    body), and raises `TerminalKillUnprovenError` while the session is still
    present;
  - it marks `exited` only after that, still under the lock.
  The presence check is the root-cause fix for the ignored tmux `kill_session`
  result, and `TmuxTerminalRuntime.terminate` is unchanged. Native proof lives
  in `terminate`: a failed kill of a current-epoch row already raises, and the
  strict stale branch below raises unless it proves the kill or the absence.
  Callers already treat a raised kill as not settled:
  - `terminal_kill` surfaces it as `TerminalTerminationError`;
  - the lifecycle route and the terminal websocket report it;
  - `WorkspaceOps._kill` and the 1.1 release move the row to `orphaned`.
- `NativeTerminalRuntime.find_host_terminal(terminal_id, spawn_key) -> str | None`
  is a strict probe. It lists the host's terminals and returns the host id of the
  row matching `(terminal_id, spawn_key)`, or `None`. It raises when the host
  cannot answer. Unlike `is_live`, it never swallows the error. Size:
  `native_runtime.py` is 854 lines, so the probe goes into a new
  `terminals/native_host_probe.py` as `NativeHostProbeMixin`, together with the
  moved `rebind_prepared`, the other host-listing lookup, and
  `NativeTerminalRuntime` inherits it. `native_runtime.py` shrinks, and callers
  keep calling `runtime.rebind_prepared` unchanged.
- `NativeTerminalRuntime.terminate`'s stale branch, taken when the row's epoch is
  missing or differs from the client's, becomes strict. As-is it reads the
  client's epoch before connecting, then reaps any recorded process and returns
  without looking at the host. The branch now connects first (`_ensure`) and only
  then compares epochs, so an unconnected client after a restart never makes a
  current row look stale. It then calls `find_host_terminal`:
  - the current host lists the terminal: it kills that host id under the current
    epoch and raises if the kill fails;
  - the listing does not contain it: it reaps the recorded process when there is
    one and returns. This is the #22530 path (I3), and absence from a strict
    listing with no claim held is proof (I1);
  - the host cannot answer, or the connect fails: the dead-group proof is
    admitted only for a usable process identity, meaning a `process` mapping
    whose `pgid` is a positive integer. `recorded_process_group_is_alive`
    (`host_reap.py:39-49`) also answers false for `None` or an invalid `pgid`,
    so that false is never read as death. With a usable identity, it reaps and
    returns only when `recorded_process_group_is_alive` is then false. No
    process, a host id with no process, or an invalid `pgid` re-raises the host
    error, and `kill_terminal` reports the row not settled.
  A `pending` row, which carries no epoch, takes the same branch, so
  `kill_terminal` needs no separate refusal for it. The branch stays in `native_runtime.py`, which keeps
  the existing `reap_recorded_process` patch target, and calls the probe it
  inherits from the mixin.
- `TerminalSettlementMixin.mark_exited_attempt(terminal_id, *,
  attempt_generation, attempt_started_at)` is a CAS from `live` or `orphaned` to
  `exited` predicated on the attempt pair, the guarded form of `mark_exited`
  (`terminal_settlement.py:266`), which has no attempt predicate. The 1.2 owner
  uses it for every `exited` settlement of a `live` or `orphaned` row.
- `TerminalSettlementMixin.record_orphan_identity(terminal_id, *,
  attempt_generation, attempt_started_at, locator, locator_key, host_epoch,
  process)` is a CAS on `orphaned` only, predicated on the attempt pair.
  Reconcile passes the pair of the row it reread under `settle_lock`. `SupportsIdentityLookup`, the
  protocol reconcile takes its manager as, already declares `settle_lock` and
  gains `record_orphan_identity`, so the new reconcile call stays inside its
  declared surface. It records every identity
  known, so that a later `terminal_kill` can address the session or reap its
  process group.
- `reconcile_host_inventory`:
  - both loops skip a held id, so reconcile never declares absence final while a
    local prepare can still create after the snapshot;
  - the first loop gains an `orphaned` branch. Under `settle_lock` it rereads
    the row. If the row is still `orphaned` with the same `spawn_key` and attempt
    generation and is not held, it calls `record_orphan_identity` with the host
    id, the current epoch and the host row's `pgid`/`start_time` when present. It
    does not kill: the row stays listed, and `terminal_kill` now addresses it;
  - the second loop is unchanged. A row the 1.2 owner kept without identity is
    `pending`, so the existing `pending` branch settles it from the strict
    listing after `spawn_in_doubt_seconds`, and the stale-epoch `orphaned`
    branch stays as it is (I3).
- `LifecycleReconciliation.reap_stale_pending` skips a held id. In 1.2, which
  already splits `spawn_executor.py`, the executor's
  `reap_stale_pending_terminals` gets the same skip and the strict absence
  proof, and the lifecycle reaper delegates to it.

**Granularity:** seven production files, one outcome: invariants I1-I3 hold for
every path that can settle a terminal. The registry, the kill proof, the strict
stale branch, reconcile and the reaper skip each close a path the others
assume closed. Landed apart, a reconcile or kill could settle a held row on
unproven state between commits. `native_host_probe.py`
is the size split that keeps `native_runtime.py` under the ceiling.

**Research context:**
- `reconcile_host_inventory` (`host_reconcile.py:122-278`) runs on host connect
  (`host_manager.py:494`, `native_runtime.py:797`). The first loop has
  `pending`/`committed` promote and `pending`/`prepared` kill-or-merge branches,
  and nothing for `orphaned`. The second loop's `orphaned` branch settles a row
  whose epoch differs from the current one and whose recorded process group is
  dead, and a NULL epoch differs.
- `LifecycleReconciliation.reap_stale_pending`
  (`lifecycle_reconciliation.py:286-297`) calls `fail_pending` directly, separately
  from `reap_stale_pending_terminals`.
- `settle_lock` is a per-terminal asyncio lock (`terminal_settlement.py:121`),
  already shared by the executor (`spawn_executor.py:744`) and reconcile.
- `PreparedSpawn` carries `process: ProcessIdentity | None` (`runtime.py:61-68`).
- `kill_terminal` callers: `servers/routes/sessions/lifecycle.py:381`,
  `servers/websocket/terminal_ws_create.py:173`, `terminal_kill` in
  `termination.py:106`, and `WorkspaceOps._kill` in `workspace_ops.py:821`.
- #22932 (Lane 3) repairs the upstream `prepare_terminal_spawn` stage. This leaf's
  proofs stand without it, so it is a cross-plan note.
- Planned checks: `DATABASE_URL=<isolated hub> GOBBY_TEST_PROTECT=1 uv run pytest
  tests/terminals/test_in_doubt_kill_truth.py tests/terminals/test_host_reconcile_orphans.py
  tests/terminals/test_termination.py -v`, plus ruff and mypy on `src/`.

Consumers unchanged:
- `src/gobby/servers/routes/sessions/lifecycle.py` — no-edit-reason: already catches a raised kill and reports it.
- `src/gobby/servers/websocket/terminal_ws_create.py` — no-edit-reason: already treats a raised kill as not transitioned.
- `src/gobby/terminals/tmux_runtime.py` — no-edit-reason: `terminate` is unchanged; `kill_terminal` proves the kill with `session_present`.
- `src/gobby/storage/terminals.py` — no-edit-reason: TerminalManager inherits `record_orphan_identity` and `settle_lock` from the mixin.
- `src/gobby/terminals/host_manager.py` — no-edit-reason: calls `reconcile_host_inventory` with unchanged arguments.
- `src/gobby/runner_init/terminal_wiring.py` — no-edit-reason: uses `NativeTerminalRuntime`'s instance API, which is unchanged; `rebind_prepared` is inherited from the mixin.
- `src/gobby/terminals/write_coordinator.py` — no-edit-reason: uses `NativeTerminalRuntime`'s instance API, which is unchanged; `rebind_prepared` is inherited from the mixin.
- `tests/agents/test_native_spawn.py` — no-edit-reason: unplaced native spawn and reconcile of `pending` rows; the runtime API and those reconcile branches are unchanged.
- `tests/mcp_proxy/tools/spawn_agent/test_response.py` — no-edit-reason: uses `NativeTerminalRuntime`'s instance API, which is unchanged; `rebind_prepared` is inherited from the mixin.
- `tests/terminals/acceptance/conftest.py` — no-edit-reason: uses `NativeTerminalRuntime`'s instance API, which is unchanged; `rebind_prepared` is inherited from the mixin.
- `tests/terminals/test_backend_selection.py` — no-edit-reason: uses `NativeTerminalRuntime`'s instance API, which is unchanged; `rebind_prepared` is inherited from the mixin.
- `tests/terminals/test_runtime_contract.py` — no-edit-reason: uses `NativeTerminalRuntime`'s instance API, which is unchanged; `rebind_prepared` is inherited from the mixin.
- `tests/terminals/test_write_input.py` — no-edit-reason: uses `NativeTerminalRuntime`'s instance API, which is unchanged; `rebind_prepared` is inherited from the mixin.
- `tests/terminals/test_host_manager.py` — no-edit-reason: host connect still calls `reconcile_host_inventory` with unchanged arguments; its existing branches are unchanged.
- `src/gobby/agents/lifecycle_monitor.py` — no-edit-reason: calls `reap_stale_pending` with an unchanged signature.

**Acceptance:**

- 1.9.1 - `claim`, `holds`, `defer` and `release` track one id; `defer` on an unheld id returns false; `release` returns the deferred steps once, and a `defer` after it returns false. `holds` called from a worker thread while the loop claims and releases answers consistently. test: `tests/terminals/test_in_doubt_kill_truth.py::test_in_doubt_registry_claims_defers_and_releases`.
- 1.9.2 - `kill_terminal` on a held id raises `TerminalInDoubtError`, calls no runtime terminate and leaves the row unchanged. After `release`, the same call kills the terminal and marks it `exited`. test: `tests/terminals/test_in_doubt_kill_truth.py::test_kill_terminal_refuses_held_ids`.
- 1.9.3 - A tmux terminate that leaves the session present makes `kill_terminal` raise `TerminalKillUnprovenError` and leave the row unsettled. Before any reconcile, a native row with a stale non-NULL epoch that the current host still lists is killed through the host id the strict probe returns before it is marked `exited`. The same row absent from the listing is reaped and settles `exited` (#22530). With the host unreachable, it settles only when a usable recorded process (a positive integer `pgid`) has a dead group afterwards. A live group, no process, a host id with no process, and a non-integer or non-positive `pgid` each stay unsettled. A `pending` row follows the same three cases, and a current-epoch row is not treated as stale while the client is unconnected. A `kill_terminal` given a stale `Terminal` decides from the row it reread under `settle_lock`. test: `tests/terminals/test_in_doubt_kill_truth.py::test_kill_terminal_requires_proven_kill`.
- 1.9.4 - `record_orphan_identity` writes the locator, epoch and process on an `orphaned` row and changes no row in any other state. test: `tests/terminals/test_in_doubt_kill_truth.py::test_record_orphan_identity_cas`.
- 1.9.5 - After a simulated restart, an `orphaned` row whose recorded host identity is stale and that the host lists gets the current host identity and process from reconcile, and `terminal_kill` then calls the host kill before it marks the row `exited`. A listed row that reconcile finds held, or whose state or attempt pair changed under the lock, is untouched. `mark_exited_attempt` and `record_orphan_identity` change no row with another attempt pair. test: `tests/terminals/test_host_reconcile_orphans.py::test_reconcile_recovers_orphan_identity`.
- 1.9.6 - `find_host_terminal` returns the matching host id or `None`, and raises when the host is unreachable. `LifecycleReconciliation.reap_stale_pending` skips a held `pending` row older than `spawn_in_doubt_seconds`. test: `tests/terminals/test_in_doubt_kill_truth.py::test_probe_is_strict_and_reaper_honors_claims`.

## P2: gclient placement reconciliation
`kind: framing`

Client-side handling of daemon-placed terminals.

### 2.1 gclient reconciliation of daemon-placed terminals [category: code] (depends: 1.4)
`kind: deliverable`

Targets:
- `crates/gclient/src/app/apply.rs::apply_event`
- `crates/gclient/src/app/live.rs::apply_live_event`
- `crates/gclient/src/app/live_workspace.rs::apply_live_workspace_event`
- `crates/gclient/src/app/live_loop/projection.rs::project_live_workspace`
- `crates/gclient/tests/placed_agent.rs`

gclient already skips an unplaced open when `pane_for_terminal` knows the terminal
(`apply_event`), and `ensure_live_pane` reuses a pane by terminal id. This deliverable
makes the live path hold that for daemon-placed terminals in every order. It reuses
the existing workspace model and projection and adds no placement registry.

- Bind first: the `tab.created`/`pane.added` event that binds the terminal reaches
  `apply_live_workspace_event` before the `created` lifecycle event.
  `pane_for_terminal` cannot see that pane yet, because it scans instantiated panes
  only. The `created` handler in `apply_live_event` therefore also asks
  `WorkspaceModel::pane_ref_for_terminal`, and when the daemon model already binds the
  terminal it opens no unplaced surface. `project_live_workspace` places the terminal
  in the bound daemon pane.
- Created first: `ensure_live_pane` has already opened an unplaced surface when the
  binding event arrives. `apply_live_workspace_event` records the bound pane as placed
  through the `placed_panes` path that client-side placement already uses. Then
  `project_live_workspace` reuses the open terminal pane in the daemon pane's slot and
  drops the local surface.
- A `created` event for a terminal that is already bound opens nothing new.
- Startup and reconnect: `reconcile_subscribe_first` attaches the workspace snapshot,
  fetches the live roster, then projects. A snapshot that already binds a live
  terminal projects it into its saved pane with no local surface.

**Research context:**
- The live event path, the model lookup and the reconnect flow are in the Evidence
  section. The `created` handlers are `apply_event` (`crates/gclient/src/app/apply.rs`)
  and `apply_live_event`, then `ensure_live_pane` (`crates/gclient/src/app/live.rs`).
- Client-side placement for user spawns is `placement_op`
  (`live_loop/workspace_actions.rs`), which is unchanged.
- The daemon binds before exec (1.2), so the workspace event is normally emitted
  first. The lifecycle and workspace streams are separate, which is why both orders
  are tested.
- Tests drive the real path: `apply_live_event`, the workspace model generation, the
  projection, and the final Chrome state, against the isolated `mock_daemon` harness
  that `crates/gclient/tests/arrange.rs` uses. Tests limited to `apply_event` would not
  prove the live behaviour.
- Load the `rust` skill and `crates/CLAUDE.md` before editing.
- Planned checks: `cargo test -p gobby-client --test placed_agent`, `cargo clippy -p gobby-client`.
  A crate change goes live only after a rebuild and install through
  `promote_workspace_binary_set`, and gclient is promoted separately (it is not in the
  stamped set). A PD slot is required.

**Acceptance:**

- 2.1.1 - A binding workspace event that arrives before `created` means the terminal opens only in its bound daemon pane, with no local surface in the final Chrome state. test: `crates/gclient/tests/placed_agent.rs::bind_then_created_opens_once_in_pane`.
- 2.1.2 - A `created` event that arrives before the binding workspace event gets its unplaced surface moved into the bound daemon pane, leaving exactly one surface and no local tab. test: `crates/gclient/tests/placed_agent.rs::created_then_bind_moves_into_pane`.
- 2.1.3 - A client that starts or reconnects through `reconcile_subscribe_first`, with a snapshot holding the bound pane and a roster holding the already-live terminal, shows exactly one surface in the saved pane, no local surface, and stable focus. test: `crates/gclient/tests/placed_agent.rs::reconnect_projects_bound_terminal_once`.

## P3: Placement pipeline acceptance
`kind: framing`

An isolated proof that an ordinary pipeline places two seats through
`spawn_agent`. It is a test fixture, not a published runbook. The tagged,
roster-guarded operator runbook is #22895's.

### 3.1 Two-seat placement acceptance fixture [category: code] (depends: 1.4, 2.1)
`kind: deliverable`

Targets:
- `tests/workflows/fixtures/two_seat_placement.yaml`
- `tests/workflows/test_placed_pipeline_fixture.py`

Add a test-owned placement fixture, `two-seat-placement-fixture`: an ordinary
pipeline with no new schema and two `mcp` steps. It lives under `tests/`, so
only the isolated test runtime imports it. There is no bundled template, and
nothing publishes it to a live pipeline registry. It carries no `runbook` tag
and is not the runbook sample.

1. `seat_a`: `gobby-agents:spawn_agent` with
   `placement: {tab: {workspace: ${{inputs.workspace}}, title: ${{inputs.seat_a_title}}}}`.
2. `seat_b`: `gobby-agents:spawn_agent` with
   `placement: {split: {pane: ${{steps.seat_a.output.pane_ref}}, axis: right, title: ${{inputs.seat_b_title}}}}`.

Each step's failure stops the pipeline through the existing step error semantics.
The fixture carries only inputs, the two steps, and prompts and agents supplied as
inputs. It carries no guard logic of its own. The refusals it exercises are
`spawn_agent`'s placement guards. `seat_live` is keyed only by workspace and tab
title, so the fixture proves placement-level refusal, not roster-level refusal;
roster guarding belongs to #22895's runbook.

The isolated acceptance test runs this pipeline end to end against an isolated test
daemon (temporary state and ports, test hub `DATABASE_URL`, `GOBBY_TEST_PROTECT=1`).
Provider processes are replaced by a stub command, and the SRT wrap is exercised with
the managed wrapper stubbed at the SRT binary boundary.

**Research context:**
- Pipeline `mcp` steps run through the tool proxy with the child session seeded.
  `_inject_agent_parent_session_argument` fills in `parent_session_id` (decision 8).
  Step outputs are addressable as `${{steps.<id>.output.<field>}}`.
- The test imports the fixture into the isolated daemon's database with
  `workflows/imports.py::sync_imported_workflow_file`, which upserts one pipeline
  YAML for a project scope. A file under `src/gobby/install/shared/workflows/pipelines/`
  would instead sync to every daemon's registry, which is why the fixture is not
  bundled.
- Existing executor test patterns: `tests/workflows/test_pipeline_executor_child_session.py`.
- After the import, inside the isolated daemon only, the CLI entry is
  `gobby pipelines run two-seat-placement-fixture --input ...` with the
  system-session parent. Cron would use the same pipeline with the cron-session
  parent. The MCP entry is `run_pipeline` with the calling-session parent.
- Planned check: `DATABASE_URL=<isolated hub> GOBBY_TEST_PROTECT=1 uv run pytest
  tests/workflows/test_placed_pipeline_fixture.py -v`. It needs a PD slot because it
  starts an isolated daemon.

**Acceptance:**

- 3.1.1 - The two-seat pipeline places seat A in a new titled tab and seat B in a right split of seat A's pane, stored with axis `horizontal`. Both terminals are SRT-wrapped and bound before exec, and both replies carry pane refs. test: `tests/workflows/test_placed_pipeline_fixture.py::test_two_seat_tab_and_split`.
- 3.1.2 - Re-running the pipeline while seat A is live fails at `seat_a` with `seat_live` and spawns nothing. test: `tests/workflows/test_placed_pipeline_fixture.py::test_rerun_refuses_live_seat`.
- 3.1.3 - An invalid pane ref for seat B fails the step with no spawn, and seat A is unaffected. test: `tests/workflows/test_placed_pipeline_fixture.py::test_invalid_ref_refuses_without_spawn`.
- 3.1.4 - An SRT wrap failure for seat B fails the step, leaves no seat B pane and no provider process, and leaves seat A unaffected. test: `tests/workflows/test_placed_pipeline_fixture.py::test_wrap_failure_refuses_seat`.
- 3.1.5 - A CLI-started run parents both agents to the pipeline child session, whose parent is the system session, and resolves the pipeline's project. test: `tests/workflows/test_placed_pipeline_fixture.py::test_cli_run_parent_and_project`.

## P4: Definition network policy and the vendored Trusted seed
`kind: framing`

Decision 14 puts network policy on the agent definition itself. 4.1 vendors the
Trusted seed. 4.2 adds the `network` field with its sync-only write guard. 4.3
resolves the field at spawn inside the 1.8 gate. 4.4 documents it. 4.1 and 4.2
are independent, and 4.3 consumes both and 1.8.

### 4.1 Vendored Trusted seed and its reviewed refresh [category: config]
`kind: deliverable`

Targets:
- `src/gobby/agents/sandbox_domains.py::*` — scope-reason: add the cached `trusted_domains()` loader beside the existing domain groups
- `src/gobby/data/sandbox/trusted_domains.json`
- `scripts/refresh_trusted_domains.py`
- `tests/agents/test_trusted_domains.py`
- `tests/scripts/test_refresh_trusted_domains.py`

Consumers unchanged:
- `src/gobby/agents/sandbox_policy.py` — no-edit-reason: `allowed_domains` already composes the provider domains, `config.allowed_domains`, the `api_base` host, the git and registry groups and loopback, and dedupes them; 4.3 puts the seed into `config.allowed_domains`.

**Research context:**
- `sandbox_domains.py` (32 lines) holds `GIT_DOMAINS` and
  `PACKAGE_REGISTRY_DOMAINS`. Package data already ships `gobby/data/**/*`
  (`pyproject.toml` `[tool.setuptools.package-data]`), and
  `cli/installers/postgres.py` reads it through
  `importlib.resources.files("gobby").joinpath("data/...")`.
- Seed source: the markdown source of the Trusted list,
  `https://code.claude.com/docs/en/cloud-environments.md`, section
  `## Default allowed domains` (checked 2026-09-27; the Researcher counted 146
  entries in 17 categories). Each category is an `<Accordion title="...">` block of
  `* ` bullets. Some bullets are markdown links (`[www.github.com](http://www.github.com)`)
  or carry an escaped dot (`raw\.githubusercontent.com`), and wildcard entries are
  written `* *.gcr.io`.
- SRT's domain rule (VERIFIED by the Researcher in
  `~/.gobby/tools/srt/0.0.76/.../dist/sandbox/sandbox-config.js:14-40,49`): an
  entry is a domain, `*.` followed by at least two labels, `localhost`, or a
  bracketed IPv6 address. It carries no scheme, path or port, and `*` and `*.com`
  are rejected. One bad seed entry would therefore fail every `trusted` launch
  closed.

**Approach:**
- `src/gobby/data/sandbox/trusted_domains.json` holds `source_url` (the page
  above, with the `#default-allowed-domains` anchor), `fetched_at` (UTC date),
  `sha256` and `categories`, an ordered map from category title to its domain
  list. `sha256` is the hex digest of the UTF-8 domain list in file order, one
  domain per line with a trailing newline.
- `sandbox_domains.trusted_domains() -> tuple[str, ...]` loads the file once
  (`functools.cache`) and returns the flattened domains in file order. It never
  fetches. A missing or unreadable file raises, so a `trusted` launch fails
  closed.
- `scripts/refresh_trusted_domains.py` is the only writer of the seed. It fetches
  the markdown source and extracts the section with
  `parse_default_allowed_domains(markdown) -> dict[str, list[str]]`: each
  `<Accordion title=...>` starts a category, each bullet drops its `* ` marker,
  a markdown link is reduced to its text, and backslash escapes are removed. Every
  parsed entry is checked against the SRT rule, and any offender makes the script
  exit nonzero with the offenders listed. The script prints a unified diff
  against the vendored list and writes nothing unless `--write` is passed; with
  `--write` it rewrites the file with a fresh `fetched_at` and `sha256`. The
  refreshed file lands through review like any other change. The executor
  creates the initial file with `--write` and commits the diff.
- Rejected: putting the seed into daemon config, which would put about 146
  domains into the config contract and let an operator edit drift from the
  recorded provenance; fetching at runtime (decision 13).

Verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_trusted_domains.py tests/scripts/test_refresh_trusted_domains.py -q`.

**Acceptance:**

- 4.1.1 - The vendored seed records its source URL and fetch date, its `sha256` matches its domain list, and every entry passes SRT's domain rule. symbol: `trusted_domains`. test: `tests/agents/test_trusted_domains.py::test_seed_provenance_and_entries_are_valid`.
- 4.1.2 - The refresh script parses the Accordion bullet format, including link and escaped entries, rejects an entry SRT would refuse, prints a diff and writes only with `--write`. test: `tests/scripts/test_refresh_trusted_domains.py::test_parse_diff_and_write_gate`.

### 4.2 Definition network field and sync-only write guard [category: code]
`kind: deliverable`

Targets:
- `src/gobby/workflows/agent_models.py::AgentDefinitionBody`
- `src/gobby/storage/definitions/agents.py::*` — scope-reason: one guard covers every non-sync write entry point of `AgentDefinitionManager` (create, update, _write_update, toggle_enabled, restore, duplicate, upsert_with_steps, set_step_workflow, move_to_project, move_to_global)
- `src/gobby/agents/sync.py::*` — scope-reason: move the new-row branch of `sync_bundled_agents` onto `upsert_from_sync`
- `tests/storage/definitions/test_agents_manager.py::*` — scope-reason: add the write-guard tests beside the existing manager tests
- `tests/agents/test_agents_sync.py::*` — scope-reason: add the network sync tests to `TestSyncBundledAgents`
- `tests/workflows/test_workflows_sync.py::*` — scope-reason: add the imported-YAML refusal test beside the existing import sync tests

Consumers unchanged:
- `src/gobby/ask/agents.py` — no-edit-reason: Ask builds bodies without `network`; the field defaults to `none`.
- `src/gobby/servers/routes/agents.py` — no-edit-reason: its writes end in `AgentDefinitionManager`, where the guard refuses a widened network.
- `src/gobby/workflows/imports.py` — no-edit-reason: writes through the manager, which carries the guard.
- `src/gobby/workflows/definitions.py` — no-edit-reason: validates bodies; a defaulted field changes nothing.
- `docs/evidence/wiki-bakeoff-code-2026-09/test_ask_cohort.py` — no-edit-reason: archived evidence, not a live consumer.
- `src/gobby/mcp_proxy/tools/spawn_agent/_step_state.py` — no-edit-reason: reads step fields only.
- `src/gobby/workflows/step_instances.py` — no-edit-reason: reads step fields only.
- `tests/agents/test_merge_orchestrator_contract.py` — no-edit-reason: builds bodies without the defaulted field.
- `tests/mcp_proxy/tools/skills/test_list_skills.py` — no-edit-reason: builds bodies without the defaulted field.
- `tests/workflows/test_agent_models.py` — no-edit-reason: existing model tests stay valid with a defaulted field.
- `tests/workflows/test_agent_workflow_completion.py` — no-edit-reason: builds bodies without the defaulted field.
- `tests/workflows/test_handler_route_lint.py` — no-edit-reason: builds bodies without the defaulted field.
- `tests/workflows/test_step_enforcement.py` — no-edit-reason: builds bodies without the defaulted field.
- `tests/workflows/test_step_instances.py` — no-edit-reason: builds bodies without the defaulted field.
- `tests/workflows/test_workflows_dry_run.py` — no-edit-reason: builds bodies without the defaulted field.

**Research context:** facts from the Researcher (gobby#14550) on 0.5.0
`8965cc963f`, and the #22899 R8 write-path evidence rechecked on `c52269ccd5`.
- `AgentDefinitionBody` (`workflows/agent_models.py`, 219 lines) has no sandbox
  or network field and sets `extra="ignore"`, so a misspelled key disappears
  silently. There is no separate profile object: the definition is the agent's
  profile.
- Every definition write ends in `AgentDefinitionManager`
  (`storage/definitions/agents.py`, 740 lines): `create` `:270`, `update` `:350`,
  `update_from_sync` `:357` over `_write_update` `:360`, `toggle_enabled` `:402`,
  `restore` `:461`, `move_to_project` `:513`, `move_to_global` `:526`,
  `duplicate` `:539`, `upsert_with_steps` `:554`, `upsert_from_sync` `:628`,
  `set_step_workflow` `:715`. The MCP definition tools
  (`mcp_proxy/tools/workflows/_agents.py:148-396`), the HTTP routes and
  `workflows/imports.py` all call these. Only `agents/sync.py` calls
  `upsert_from_sync` (`:209`, `:236`), and nothing outside the manager calls its
  `update_from_sync`.
- #22965 landed at `b024c6e096` (activation pending); once active, the MCP
  definition tools and HTTP routes refuse agent-token mutation requests for
  every field. 4.2 does not depend on that endpoint actor check: the storage
  guard alone keeps `trusted` sync-only. The HTTP request models accept `tags`,
  so the bundled-row marker can be forged too. The storage guard is therefore
  the only sound place to stop a widened network.
- A second writer also stores rows as `source="installed"`:
  `workflows/imports.py::sync_imported_workflows` (`:202`) reads
  `~/.gobby/workflows` and `<project_path>/.gobby/workflows/agents/*.yaml`
  (`:209-213`), and `_upsert_agent` (`:65-91`) writes through the ordinary
  `update` and `upsert_with_steps`. An agent can trigger it through
  `gobby-workflows:reload_cache(project_path=...)`
  (`mcp_proxy/tools/workflows/_import.py:16-37`) after writing a YAML in its own
  worktree (Researcher gobby#14550, code paths VERIFIED, exploit INFERRED).
- `sync.py` (299 lines, 0.5.0 `8965cc963f`) creates new rows through
  `upsert_with_steps` at `:258`, its one non-sync write. It updates and restores
  tombstoned bundled rows through `upsert_from_sync` (`:209`, `restore=True`).
- The executor reruns `gcode grep -w upsert_with_steps src/`, the same grep for
  `.update(` over the HTTP agents routes module, and
  `gcode grep -w create_agent_definition src/` before editing, and adds any new
  write path to the guard.

**Approach:**
- Add `network: Literal["none", "trusted"] = "none"` to
  `AgentDefinitionBody`, described as "Egress for the agent's SRT sandbox;
  `trusted` is set only by bundled templates". `none` adds nothing to
  the daemon's `agent_sandbox` policy (decision 14). `trusted` adds the vendored
  Trusted seed and the git and registry groups (4.3).
- In `storage/definitions/agents.py` add one private guard,
  `_refuse_network_write(incoming_body, current_row)`. It raises
  `ValueError("network is sync-owned")` when the incoming parent body carries a
  `network` other than `none`, or the locked current row's body does. Every
  non-sync entry point in the scope-reason calls it inside the transaction that
  holds the row lock; `update_from_sync` and `upsert_from_sync` skip it. A row
  with a widened network is therefore immutable outside the sync, whether the
  edit is a rename, a rule patch, a restore or a re-tag.
- Switch the new-row branch of `sync_bundled_agents` to `upsert_from_sync`, so
  a bundled template that declares `trusted` can be created.
- Writer classification: only `agents/sync.py::sync_bundled_agents`, reading
  the bundled `src/gobby/install/shared/workflows/agents/`, may write a
  `network` other than `none`, and it is the only caller of `upsert_from_sync`
  and `update_from_sync` for agent rows. `imports.py` global and project imports
  write through `update` and `upsert_with_steps`, so they are refused like any
  other non-sync write, whatever `source` they store. The executor keeps
  `imports.py` off the sync entry points.
- Rejected: per-route checks (every route ends in the manager); comparing the row
  with the bundled YAML at launch (a second mechanism that fails closed on an
  unsynced template edit); a launch-time ownership check (the guard already makes
  a widened row sync-only).

Verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/storage/definitions/test_agents_manager.py tests/agents/test_agents_sync.py tests/servers/routes/test_agents_routes.py -q`,
plus the MCP definition-tool tests the executor finds with
`gcode grep -lw create_agent_definition tests/`.

**Acceptance:**

- 4.2.1 - `AgentDefinitionBody` accepts `network` of `none` or `trusted`, defaults to `none`, and rejects any other value; a bundled template's `trusted` survives sync into the stored row. symbol: `AgentDefinitionBody`. test: `tests/agents/test_agents_sync.py::TestSyncBundledAgents::test_sync_preserves_trusted_network`.
- 4.2.2 - Every non-sync write that brings in a body with `network: trusted` is refused: create, update, upsert_with_steps and duplicate. test: `tests/storage/definitions/test_agents_manager.py::test_non_sync_writes_refuse_widened_network`.
- 4.2.3 - A row with `network: trusted` is immutable outside the sync: update, toggle, restore, set_step_workflow and both moves refuse. test: `tests/storage/definitions/test_agents_manager.py::test_widened_network_row_is_immutable_outside_sync`.
- 4.2.4 - The sync creates, updates and restores rows carrying `network: trusted` through sync entry points only. test: `tests/agents/test_agents_sync.py::TestSyncBundledAgents::test_sync_new_row_uses_sync_entry_point`.
- 4.2.5 - An imported global or project agent YAML carrying `network: trusted`, loaded through `sync_imported_workflows` (the `reload_cache` path), is refused and leaves no row with a widened network. test: `tests/workflows/test_workflows_sync.py::test_imported_agent_yaml_cannot_widen_network`.

### 4.3 Resolve the definition's network at spawn [category: code] (depends: 1.8, 4.1, 4.2)
`kind: deliverable`

Targets:
- `src/gobby/agents/sandbox_network.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_sandbox_gate.py`
- `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py::spawn_agent_impl`
- `src/gobby/mcp_proxy/tools/spawn_agent/_placement.py`
- `tests/agents/test_sandbox_network.py`
- `tests/agents/test_resume_sandbox_gate.py`

Consumers unchanged:
- `src/gobby/dispatch/spawn.py` — no-edit-reason: it passes the loaded `agent_body` (a deep copy that keeps `network`) to `spawn_agent_impl` (`:357-361`).
- `src/gobby/scheduler/executor.py` — no-edit-reason: same call signature; cron definitions keep `none`.
- `src/gobby/feedback/agent.py` — no-edit-reason: same call signature; the feedback definition keeps `none`.
- `src/gobby/servers/routes/agent_spawn.py` — no-edit-reason: it returns `spawn_agent_impl`'s error payload unchanged, so a refusal surfaces as `sandbox_required`.
- `src/gobby/ask/agents.py` — no-edit-reason: it passes a managed runtime profile, whose branch stays first in `resolve_spawn_sandbox`.
- `src/gobby/agents/spawn_cache_policy.py` — no-edit-reason: `sandbox_config_for_spawn` still merges project and cache write paths onto the resolved config.

**Research context:**
- After 1.8, `resolve_spawn_sandbox` (`_sandbox_gate.py`) returns the gated config
  for `spawn_agent_impl` and `preflight_placement`, and every result ends in
  `require_managed_srt`. Network resolution goes inside it, so placed and unplaced
  launches share one path.
- Today's choice (0.5.0 `c52269ccd5`, `_implementation.py:393-398`): an Ask
  managed runtime profile's `sandbox_config` first, else
  `apply_write_grant(agent_sandbox_config(daemon_config), write_grant)`.
  `agent_sandbox_config` is SRT with `allow_network` false, so egress is the
  `allowed_domains` allowlist that `sandbox_policy.allowed_domains` composes.
  `srt_runtime.py:492` refuses `allow_network` true under `srt`, so no
  definition can open unrestricted network.
- The spawn stores the resolved config as the resume snapshot, and 1.8's
  `resolve_resume_sandbox` replays it. A resumed `trusted` run therefore keeps
  the domains it was authorized with at spawn.
- Size: `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py` (964 lines at
  `c52269ccd5`) is a split: network resolution does not enter it but moves to
  the new `src/gobby/agents/sandbox_network.py`. `_implementation.py` only
  passes the agent body to `resolve_spawn_sandbox` and does not end above its
  `c52269ccd5` size.

**Approach:**
- `src/gobby/agents/sandbox_network.py` defines
  `definition_sandbox_config(daemon_config, agent_body) -> SandboxConfig`. It
  starts from `agent_sandbox_config(daemon_config)`. For `network: trusted` it
  returns a copy with `trusted_domains()` appended to `allowed_domains` and
  `allow_git_network` and `allow_package_registries` set, so
  `sandbox_policy.allowed_domains` adds `GIT_DOMAINS` and
  `PACKAGE_REGISTRY_DOMAINS` whether or not the seed lists them; for
  `none`, or no body, it returns the base unchanged. `allow_network` stays
  false either way.
- `resolve_spawn_sandbox` keeps the Ask managed runtime profile first. Otherwise
  it uses `definition_sandbox_config(daemon_config, agent_body)`, applies the
  write grant and ends in `require_managed_srt`. A seed that cannot be loaded
  refuses with `sandbox_required` before any side effect, as 1.8 does.
  `spawn_agent_impl` and `preflight_placement` pass the agent body.
- Resume is unchanged: the snapshot carries the resolved domains, and a
  definition edit applies to later spawns.
- The unreadable-seed refusal reuses `sandbox_required` deliberately: it is a
  broken install, and a new code would add a refusal path with no distinct
  caller action.
- Rejected: re-resolving the definition at resume (it adds a missing-definition
  refusal and reopens 1.8's accepted snapshot design).

Verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/agents/test_sandbox_network.py tests/agents/test_resume_sandbox_gate.py tests/mcp_proxy/tools/spawn_agent/test_sandbox_gate.py -q`,
plus ruff and mypy on `src/`. The new spawn test module opts into
`stub_srt_verifier` (1.8) with a module `pytestmark`.

**Acceptance:**

- 4.3.1 - A spawned `trusted` definition resolves to enabled SRT with `allow_network` false whose domains are the Trusted seed, the git and registry groups and the Gobby hosts, and a `none` definition gets exactly the `agent_sandbox` policy plus the write grant, placed or not. symbol: `definition_sandbox_config`. test: `tests/agents/test_sandbox_network.py::test_trusted_definition_adds_seed_to_srt_allowlist`.
- 4.3.2 - An unreadable seed refuses a `trusted` spawn with `sandbox_required` before any side effect, and a `none` spawn does not read the seed. test: `tests/agents/test_sandbox_network.py::test_unreadable_seed_refuses_trusted_spawn_only`.
- 4.3.3 - A resumed `trusted` run replays its spawn-time domains through the 1.8 snapshot path. test: `tests/agents/test_resume_sandbox_gate.py::test_resume_replays_trusted_snapshot`.
- 4.3.4 - `_implementation.py` does not end above its `c52269ccd5` size. file: `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`.

### 4.4 Document definition network policy in the sandboxing guide [category: docs] (depends: 4.3)
`kind: deliverable`

Targets:
- `docs/guides/sandboxing.md`

**Research context:** `docs/guides/sandboxing.md` documents both backends, the
`agent_sandbox` and `web_chat_sandbox` parameter table, and the order in which
project `extra_write_paths` merge.

**Approach:** Add an "Agent network policy" section covering:
- the definition's `network` field, `none` and `trusted`, and that neither
  disables SRT or opens unrestricted network;
- the vendored Trusted seed, its provenance fields and the reviewed
  `scripts/refresh_trusted_domains.py` refresh;
- the sync-only write rule for `trusted`;
- the `sandbox_required` refusal and snapshot replay on resume;
- that the Trusted list is a developer-dependency allowlist, not general web
  access (decision 13).

**Acceptance:**

- 4.4.1 - The guide documents the `network` field, the seed and its refresh, the write guard and the refusal. behavior: "Agent network policy" in `docs/guides/sandboxing.md`.

## V1: Verification
`kind: verification`

End-to-end check after 1.1 to 1.8, 2.1, 3.1 and 4.1 to 4.4 land, run in an isolated
environment only:
- 1.1 to 1.8 and 4.1 to 4.3 focused pytest;
- `cargo test -p gobby-client --test placed_agent`;
- the 3.1 isolated-daemon pipeline test.

After a PD-scheduled restart and gclient promotion, the placement smoke runs in an
isolated scratch daemon, never the live one. The daemon runs from a throwaway
worktree of the landed commit with its own database, state and ports
(`GOBBY_ALLOW_WORKTREE_DAEMON=1`, announced as testing). The fixture is copied to
the scratch project's `.gobby/workflows/pipelines/two_seat_placement.yaml`, and
`gobby-workflows:reload_cache` on the scratch daemon imports it through
`sync_imported_workflows`. Then:

```sh
gobby pipelines run two-seat-placement-fixture
```

It runs against a scratch workspace and must show two placed, SRT-wrapped seats and a
refused re-run. Nothing is imported into the live registry. The operator-facing
guarded runbook smoke belongs to #22895. Researchers never touch the live daemon or
its seats.

SRT smoke, after P4 lands, in an isolated Program Director slot. This plan
assigns `trusted` to no production definition (decision 14); that assignment
stays with Josh and #22902 and is not a prerequisite of this smoke. The smoke
brings its own sync-owned fixtures:
- Fixtures. The slot runs its isolated daemon, with its own database and ports,
  from a throwaway worktree of the landed commit
  (`GOBBY_ALLOW_WORKTREE_DAEMON=1`, announced as testing). Before start, the
  worktree's `src/gobby/install/shared/workflows/agents/` gains two uncommitted
  files, each a copy of the bundled `default.yaml`
  with its `name` changed:
  - `smoke-network-trusted.yaml` adds `network: trusted`;
  - `smoke-network-none.yaml` adds no `network` key.
  Startup `sync_bundled_agents` writes both as sync-owned rows in the slot
  database only. The worktree and database are discarded after the smoke, and
  nothing is committed.
- Probe host. The smoke derives one host from the slot's resolved policy: a host
  in the vendored Trusted seed that is absent from the provider API domains, the
  `api_base` host, the slot's operator `agent_sandbox.allowed_domains`,
  `GIT_DOMAINS` and `PACKAGE_REGISTRY_DOMAINS`. It records the host and that set
  difference with the result. If the difference is empty, the negative probe is
  reported as not runnable, never as passed.
- A spawned `smoke-network-trusted` records `sandbox_enabled=true` and backend
  `srt` and reaches the probe host. A spawned `smoke-network-none` records the
  same sandbox fields and cannot reach the probe host, while it can still reach
  a host from the slot's operator `allowed_domains` when that list is non-empty.
  That keeps `none` equal to the `agent_sandbox` policy, operator hosts included.
- its WebFetch to a host outside the allowlist fails (confirms the named gap);
- `brave-search` through the MCP proxy works;
- a REST or CLI MCP call is rule-enforced;
- reads of `local_cli_token` and `bootstrap.yaml` fail;
- an `enabled: false` config is refused;
- the vendored seed's sha256 matches its recorded provenance.

**Round 1 of 1** `kind: enhancement`

- enhancer_run: a3d33670-527b-454c-87b6-280d8ef8b9f2 (plan-enhancer-taskless, isolation none)
- enhancer_session: cbcdadc7-a9c0-430f-8b91-5bd9ea36a99d
- round: 1 of 1 (cap 1)
- plan_head_reviewed: 4b9eb0e523
- converged: false
- suggestions_presented: 9, each with its full description, suggested_enhancement and
  metadata, to the Program Director gobby#14610 on 2026-09-27
- presented_by: Plan Writer gobby#14578, after checking every code claim against 0.5.0
- votes (all ACCEPT, ruled individually by the Program Director):
  - placed-launch-better-failure-compensation: accept. Typed failures return
    `success: false` from `finalize_executed_spawn` and never reach `_spawn_failure`,
    so the old routing leaked the pane. Folded into decision 5, the Evidence failure
    paragraph and 1.4 (the `run_placed_spawn` boundary, 1.4.3 to 1.4.5).
  - placed-launch-better-seat-key: accept. The per-kind lookup allowed a tab seat and
    a split seat with one title in one workspace. Folded into decision 7 and 1.1
    (1.1.3, 1.1.6).
  - placed-launch-better-source-contracts: accept. `add_pane` accepts only
    `horizontal`/`vertical`, and the old 1.2 text put the SRT wrap after the bind.
    Folded into decision 3, the Evidence axis bullet, 1.1 (1.1.7), 1.2 (1.2.1) and
    3.1.1.
  - placed-launch-better-project-provenance: accept. The factory collapses the
    project source before `spawn_agent_impl`, so the `parent_unresolved` rule could
    not be implemented as written. Folded into decision 8 and 1.4 (the provenance
    sibling of `_resolve_spawn_project_context`, 1.4.8).
  - placed-launch-better-composition: accept. No workspace service reaches
    `create_spawn_agent_registry`, so a per-call reserver would defeat the
    per-workspace lock. Folded into the Evidence composition bullet and the new 1.3,
    which reuses the `workspace_ops_resolver` pattern.
  - placed-launch-better-gclient-live-path: accept. The live workspace path runs
    through `apply_live_workspace_event` and `project_live_workspace`, which the old
    P2 did not target. Folded into the Evidence gclient bullet and 2.1.
  - placed-launch-better-reserve-isolation-context: accept. Preflight runs before
    isolation, so it cannot know a fresh worktree id. Folded into 1.1
    (`reserve(resolved, *, worktree_id)`, 1.1.8) and 1.4.
  - placed-launch-better-granularity: accept. The old 1.2 had nine acceptance items,
    seven production Targets and two lifecycle owners with no Granularity record. It
    is split at the `SpawnRequest` boundary into three independently closeable
    sections, each with a Granularity record where a count trigger applies: 1.2 (the
    executor bind hook, inert without a caller), 1.3 (the daemon-scoped reserver
    composition, inert without a caller) and 1.4 (the placement input, preflight,
    reservation, compensation and reply, with the end-to-end placed-spawn acceptance
    1.4.10). The executor hook lands first so every leaf is safe to land alone.
    Reservation moves to dispatch inside 1.4, so the compensation boundary lives
    entirely in `_placement.py`. Old acceptance IDs map as follows: 1.2.1 to 1.4.1,
    1.2.2 to 1.4.2, 1.2.3 to 1.2.1, 1.2.4 to 1.4.3, 1.2.5 to 1.4.6, 1.2.6 to 1.4.7
    and 1.2.2, 1.2.7 to 1.2.3, 1.2.8 to 1.4.8, and 1.2.9 to 1.4.9. 2.1 and 3.1 now
    depend on 1.4.
  - placed-launch-bigger-reconnect: accept. The gclient invariant covers a client
    that starts or reconnects after the agent is bound. Folded into decision 10 and
    2.1 (2.1.3), using the existing `reconcile_subscribe_first` flow.
- writer additions while folding, each from a source check and outside the nine
  suggestions: the Consumers unchanged inventories for 1.2, 1.3 and 1.4, the 1.3
  statement that every new parameter and field defaults to `None`, and the 1.2 rule
  that the moved functions import the facade helpers inside their bodies, which
  avoids a circular import and keeps facade patches effective.
- declined: none
- no new product scope was approved; each change repairs a stated invariant with an
  existing mechanism. No second enhancer pass.
- Program Director design review of 9f4209f1e1 (gobby#14610, 2026-09-27) accepted the
  three-way split and the reservation after the existing guards, and asked for four
  repairs, folded here without new scope:
  - reserve atomicity under exceptions and cancellation (decision 5, 1.1 `reserve`,
    1.1.9, 1.1.10, 1.4 step 2, 1.4.11);
  - one terminal-kill owner, with a release backstop when cleanup raises (decision 5,
    1.1 `release`, 1.1.11, 1.3, 1.4.5);
  - Overview and Constraints wording that separates the side-effect-free preflight
    from the guarded reservation;
  - the `gobby-client` cargo package in 2.1 and V1.
  The `workspace_ops.py` size is re-measured at 982 lines.
- Program Director diff review of e071a6ce72 (gobby#14610, 2026-09-27) accepted the
  Overview, order, package and reserve-level compensation, and asked for two
  failure-semantics repairs, folded here without new scope:
  - release steps that run independently from the terminal id captured at bind, so
    a removal or publish failure never skips the kill check or the mark clear
    (decision 5, 1.1 `release`, 1.1.12, 1.4 step 2);
  - reserve wording that separates successful compensation from recoverable residue
    after a failed rollback, with that residue logged and pruned by
    `sweep_dead_panes` (Overview, decision 5, 1.1 `reserve`, 1.1.13).
- Program Director review of dc724cacfa (gobby#14610, 2026-09-27) accepted the
  independent release steps and the sweep-recoverable residue, and asked for one
  ownership clarification inside the same release invariant, folded here without
  new scope: the binder records the launch-created pending terminal before it
  awaits `bind`, so a publish failure after the binding persisted still leaves
  release the owned id, and a `busy` conflict drops it so no terminal another pane
  holds is claimed (decision 5, 1.1 `bind` and `release`, 1.2, 1.2.2, 1.4 step 2
  and boundary, 1.4.5, 1.4.12).
- next: Josh's approval of the design, then the Plan Adversary.

**Plan Adversary round (PAL-01 to PAL-09)** `kind: enhancement`

- reviewed_head: 1d3d4f25a3 (Josh approved this design, relayed by the Program
  Director gobby#14610 on 2026-09-27)
- reviewer: Plan Adversary gobby#14579; nine blocking findings, all accepted
  - PAL-01 kill truth: a failed kill is `orphaned`, never `exited`, and keeps created
    isolation (decision 5, 1.6, 1.1 release).
  - PAL-02 and PAL-06 single cleanup owner: `SpawnCleanupOnce` per spawn attempt,
    independent steps, the drain pattern for cancellation (1.6, 1.4 boundary).
  - PAL-03 moved split targets: `add_pane` checks the expected workspace, tab and
    project under locks (1.5, 1.1.15).
  - PAL-04 atomic guards: close, move and swap refuse in-flight panes inside the
    storage transaction, and the mark holds until the launch settles (1.5, 1.1
    `settle`).
  - PAL-05 duplicates: a placed duplicate is `task_active`, with `seat_live`
    precedence at preflight (decision 7, 1.4 step 3, 1.4.13).
  - PAL-07 placed timeout: the row is `orphaned` at expiry and the late cleanup
    settles any state (1.2, 1.2.4).
  - PAL-08 source inventory: `_promote_prepared` and `_cleanup_timed_out_prepare`
    Targets, `_persist_spawn_workspace` among the facade imports, the droid test
    consumer (1.2).
  - PAL-09 placed resume: Program Director ruling Option A (1.7).
- follow-up constraints from the Adversary (2026-09-27), folded: both expected ids
  checked under one lock order; the seat survives a failed kill in its own pane;
  the cleanup owner's lifetime is the spawn attempt; no dependency on #22663; the
  refusal precedence.
- scope delta against the approved design: three new leaves (1.5, 1.6, 1.7); two
  reversed statements (decision 12); a shared failure-cleanup change that also
  applies to unplaced spawns (1.6); `sweep_dead_panes` keeps `orphaned` panes for
  every workspace (1.5). The Program Director decides whether Josh sees it again.
- dependencies: 1.1 on 1.5 and 1.6; 1.2 on 1.6; 1.4 adds 1.6; 1.7 on 1.4. No
  cycle, and no leaf depends on another task.
- next: the Adversary verifies these bytes; consensus, then M1 by the Adversary and
  the Program Director's confirmation.

**Program Director consolidation (2026-09-27)** `kind: enhancement`

- base: 84d2acad1a (PAL-01 to PAL-09 repairs)
- #22883 reconciliation: 1.5 no longer extracts the pane-source policy. It builds on
  #22883's `workspace_pane_access.py` extraction (gobby#14642), cited by commit
  when it lands (decision 12).
- Josh's final sandbox scope (decision 13): every `spawn_agent` launch and resume
  runs under managed SRT with no unsandboxed fallback (new 1.8); gclient hand
  launches and shells are out of scope; no seat migration. Researcher evidence:
  gobby#14550, `.gobby/plans/research/researcher-srt-egress-2026-09-27.md` rev 2.
- typed deferrals: D2 (#22961
  loopback enforcement).
- Program Director rulings (2026-09-27): the Trusted seed goes on `research` only,
  specified by #22899 (D1), and the default profile keeps its required hosts;
  web option A stays proposed with the local WebFetch gap named, and its SRT
  smoke is unrun; credential hardening and the escape-surface audit are design
  findings, not leaf scope.
- #22883 landed on 0.5.0 as `070a19c3d4` (source candidate `41622e0782`), leaving
  `workspace_ops.py` at 893 lines and the new `workspace_pane_access.py` at 148.
  The size lint still fires at 893, so 1.5 moves the pane I/O group, which it
  does not change, into `workspace_pane_io.py` (new 1.5.8; 1.5.7 now bounds
  `workspace_ops.py` under 850).
- 1.8 test fix: no spawn test patches `verify_srt_installation`, so the earlier
  research note claiming they do was wrong. After Program Director review, 1.8
  adds one named, non-autouse `stub_srt_verifier` fixture. Each affected unit
  module opts in through an explicit `pytestmark` edit, and those modules are
  now 1.8 Targets, removed from the unchanged inventories of 1.2, 1.3, 1.4 and
  1.6. Gate unit cases control the verifier per case; one new gate case runs the
  real verifier against an isolated empty `GOBBY_HOME` (1.8.6). e2e and
  integration suites are never stubbed.
- Program Director consolidated review of `e0c3a08b89` (2026-09-27): D1 no
  longer credits closed planning task #22899 with the implementation. It is a
  placeholder that expansion turns into a tail task. The 1.1 and 1.6 failure
  logs are sanitized, logging the exception type name and ids, never
  `exc_info`, message or traceback, and their tests assert a synthetic secret
  is absent (the #22962 precedent). Decision 9 names the operator, Assistant
  and pipeline model. 1.4.2's unplaced clause is marked leaf-local ahead of
  1.8.
- Josh's decision on `0713ee76fc`, relayed by the Program Director
  (2026-09-27): "Fold in wiring the profile". New decision 14 and phase P4
  replace D1:
  - 4.1 adds named SRT-only profiles, the vendored Trusted seed and its reviewed
    refresh script.
  - 4.2 adds the definition selector and the sync-only write guard.
  - 4.3 resolves profiles inside the 1.8 spawn and resume helpers and binds
    `researcher` to `research`.
  - 4.4 documents it.
  - 1.8.4 moves to 4.3.1. Researcher facts come from gobby#14550 on 0.5.0
    `c52269ccd5`.
- Program Director review of `8f94a67996`: 4.3 records its Granularity decision
  (one outcome, and the spawn/resume split was rejected). Decision 14 states the
  trust limitation outright: whoever can spawn `researcher` gets research-profile
  egress, and no new boundary is promised.
- Josh's decision on `9fc9c635b1`, relayed by the Program Director
  (2026-09-27): "Researchers aren't special. Some agents get network and some
  don't." Decision 14 is rewritten: the definition's own
  `network: none | trusted` field replaces the named-profile registry, the
  `sandbox_profile` selector, the research profile and the researcher binding.
  - 4.1 keeps only the vendored seed and its refresh; the config classes and
    carrier regeneration are gone.
  - 4.2 guards `network` instead of the selector.
  - 4.3 resolves at spawn only; resume replays the 1.8 snapshot. `trusted` also
    sets the existing git and registry flags, as the Researcher recommended.
  - `researcher.yaml` is no longer a target. #22902 decides each seat's value.
  - Facts come from the Researcher (gobby#14550) on 0.5.0 `8965cc963f`.
- Program Director review of `9fb360322a`: the V1 SRT smoke brings its own
  sync-owned fixtures, `smoke-network-trusted` and `smoke-network-none`, copied
  from `default.yaml` into a throwaway worktree that the slot's isolated daemon
  syncs. Nothing is committed, and no production definition or #22902
  prerequisite is assumed. The `none` negative probe uses a derived Trusted-only
  host that is absent from the provider, `api_base`, operator, git and registry
  allowlists. A positive operator-host check shows `none` keeps operator policy.
- Plan Adversary round on `f6947365b4` (gobby#14579). Per-id dispositions:
  - PAL-01 accepted for an actual reported kill failure only, not final.
  - PAL-02, PAL-03, PAL-05, PAL-08 and PAL-09 resolved in the Adversary's
    review.
  - PAL-04 accepted: `close` and `close_tab` take `expected_panes` and refuse
    `busy` on any membership or binding drift since the read (1.5, 1.5.9).
  - PAL-06, PAL-07 and PAL-10 accepted as one repair. New leaf 1.9 adds the
    in-doubt registry, `kill_terminal`'s refusal and proof, and
    `record_orphan_identity`. 1.2 hands an unresolved placed prepare to one
    background owner on timeout or any cancellation. The owner kills from the
    prepared handle with a proof, not `kill_spawn_key`. 1.2.4, 1.2.6, 1.2.7,
    1.4.14 and 1.7.6 cover the races.
  - PAL-11 accepted: 1.5 depends on 1.6.
  - PAL-12 accepted: both files are `::*` targets that name the moved symbols.
  - PAL-13 accepted: D2 is deleted, since #22961 landed at `08e54acb95` and
    `2c2b569fe3`. 1.8.5 is ordinary acceptance, and the decision text and the
    V1 smoke no longer wait on it.
  - The restart limits in 1.9 are recorded for the Program Director.
- Plan Adversary follow-up on `e723edbada` (gobby#14579). PAL-04, PAL-11, PAL-12
  and PAL-13 are resolved. PAL-06, PAL-07 and PAL-10 had five gaps plus the
  restart path, all accepted:
  - Close before kill: the 1.5 storage guard refuses a pane whose terminal is
    held, inside the transaction. Decisions 5 and 7 and the 1.1 release say so.
  - A prepare failure is not proof: the owner proves absence with
    `session_present` for tmux or the strict `find_host_terminal` for native
    (1.2.8).
  - Deferred isolation: cleanup defers created-isolation removal to the claim,
    and the owner runs it after a proven settlement (1.6.7, 1.4.14).
  - Process identity: the owner and reconcile record `process`, and a stale
    epoch with a dead recorded group is proven.
  - Cancellation at completion: the owner consumes a done task (1.2.6).
  - Restart (Gap R): the claim now starts at `create_pending`, and both reapers
    and reconcile honor it. Reconcile recovers an orphan's identity under
    `settle_lock`. `kill_terminal` rereads under the same lock and refuses an
    identity-less native orphan. Invariants I1-I3 are stated in 1.9, with the
    #22530 contract kept for recorded stale epochs.
  - #22932 stays a cross-plan note, a joint recommendation to the Program
    Director.
- Program Director consolidation: runbooks are ordinary pipelines owned by
  #22895. The 3.1 sample stays one pipeline, `runbook-two-seat-example`.
- Plan Adversary round on `fccbabaeb1` (gobby#14579). Per-finding dispositions:
  - A, claim ownership exits: accepted. The claim now starts before the create
    dispatch. One boundary with an exits table covers every return and raise,
    and every dispatched-prepare failure goes to the owner. The owner drains
    create, bind, reserve and promotion futures, contains storage failures by
    retrying and then keeping the claim held, and releases only after its final
    settlement write (1.2.9, 1.2.10).
  - B, stale-epoch kill truth: accepted. `terminate`'s stale branch connects
    first, then runs the strict probe. A listed terminal is killed under the
    current epoch, an absent one follows #22530, and an unreachable host
    settles only on a dead recorded group. The identity-less refusal is
    subsumed, and 1.9.3 tests the kill before reconcile.
  - C, registry threading: accepted. The registry is lock-guarded with a leaf
    lock, `holds` is supported from the DB worker thread, and the busy check
    stays inside the storage transaction.
  - Compensation ordering: `release` removes the id and captures the steps
    atomically, and each step is contained.
  - Program Director name correction: the rename is reverted, and the sample
    is `runbook-two-seat-example` again. Carrying a `runbook` tag onto the row
    is #22895's, because bundled pipeline sync writes `tags=["gobby"]` today.
- Plan Adversary round on `37ff136247` (gobby#14579). C is resolved.
  - PAL-10, process identity: accepted. The dead-group proof requires a usable
    identity (a positive integer `pgid`). No process, a host id only, or an
    invalid `pgid` stays unproven, and 1.9.3 pins each case.
  - PAL-06/07, promotion: accepted. Placed `_promote_prepared` takes
    `defer_failure`, so its failed-result and raise branches neither kill nor
    settle. Failed results go to the owner with the retained `prepared` (E12).
  - Cancellation-safe handoff: accepted. Every stage is a shielded, retained
    task, and the handoff is synchronous. Owner order is per stage: `prepare`
    orphans before draining, and `promote` drains then rereads.
  - Coverage: accepted. The exits are prose cases E1-E14 under the
    table-driven 1.2.9. 1.9 targets `SupportsIdentityLookup`, and 1.2 and 1.9
    record Granularity decisions.
  - Storage retry: the plan states what each failed write leaves. The
    unconditional release in 1.2.4 is scoped to a successful final write.
- Plan Adversary round on `bd306027eb` (gobby#14579). PAL-10, the promotion
  deferral, the shielded tasks, stage ordering, the protocol Target and the
  granularity repairs are resolved. PAL-07 storage boundary: accepted. A final
  write counts only when the read-back row confirms the required state and
  identity. A CAS that matches no row keeps the claim held, and the owner
  retries the orphan transition before recording the identity (1.2.10).
  `SupportsIdentityLookup` wording corrected: only `record_orphan_identity` is
  new.
- Plan Adversary consensus (gobby#14579, 2026-09-27) on `b4f5559124`: the
  confirmed-settlement rule closes PAL-07. PAL-06, PAL-07, PAL-10 and the earlier
  substantive findings are resolved at plan level. The final wording correction
  is in 1.2.10: when both writes fail, the claim stays held and restart recovers
  the row from the strict host inventory; no durable prepared identity is
  asserted. The Adversary derives and applies M1. The Program Director reviews
  and gates expansion after Josh approves.
- 2026-09-27: settlement retry without restart (Josh via the Program Director,
  gobby#14610: "Why wouldn't we fix the limitation"). Before this amendment,
  a claim held after the final settlement write was exhausted needed a daemon
  restart. Now the 1.2 owner keeps the claim and its decided settlement and
  retries on capped backoff (1 s doubling to 60 s, no attempt limit) until
  storage recovers. It then settles and releases once, and deferred
  compensation runs at most once. Cycles never probe, kill, re-dispatch or
  fabricate proof. Shutdown cancels the owner without releasing, and restart
  recovery stays the fallback. 1.2.10 is restated and 1.2.11 added. No new
  enhancer pass. M1 is rederived from the amended bytes after fresh Adversary
  consensus.
- 2026-09-27: Adversary round on `5ebdc3eea9` (gobby#14579), PAL-14 and PAL-15
  accepted, plus a Writer-found base defect.
  1. Base defect: `mark_kill_failed` moved `pending` to `orphaned`, an edge
     `ALLOWED_EDGES` lacks and a row the schema CHECKs reject without identity.
     The identity-less orphan is gone. An identity-less kept row stays
     `pending`, a `pending` row moves to `orphaned` only with its identity in
     one CAS, and the edge is added (1.2 step 1, 1.6, 1.9). No schema,
     carrier or migration change: the attachable-identity CHECKs stay as they
     are. The owner's absence retry re-runs the presence check and re-issues
     the unproven strict kill on the cadence. Both stale-pending reapers now
     settle only on proof, and the lifecycle reaper delegates to the strict one
     (1.2.12).
  2. PAL-14: `fail_pending_attempt` confirms as `exited` with the captured
     `attempt_generation` and `attempt_started_at`. Every re-issued write
     carries that pair (`mark_exited_attempt` added). Compensation runs after
     a confirmed no-resource settlement, including a create that committed no
     row, and a late defer with no row removes created isolation.
  3. PAL-15: the retry is a retained task behind `asyncio.shield`, so owner
     cancellation cannot stop it while the daemon lives. Only loop teardown
     cancels it, which releases nothing (1.2.13).
- 2026-09-27: Adversary round on `8881c697b5` (gobby#14579), PAL-14 remainder
  and PAL-16 accepted.
  1. PAL-14: a raised create or bump is indeterminate. E1 hands it to the
     owner, which drains the task, reads the row back under `settle_lock` and
     adopts a committed attempt's pair or confirms a rollback. It never
     re-executes the create or bump, and a raised inline settlement write goes
     the same way (1.2.14).
  2. PAL-16: 1.2.4, 1.2.7 and 1.2.8 are swept to the `pending` model. An
     unresolved or identity-less row stays `pending`. Only a prepared identity
     with an unproven kill becomes `orphaned`.
  3. Owner lifetime: one retained owner task covers the drain, recovery,
     proof, settlement, retries, release and compensation, and every inner
     step is shielded.
  4. Follow-up on `b35e808ab0`: a rolled-back bump. A failed retry result
     carries `prior_attempt`, the pre-bump pair, and a late cleanup that finds
     the row still at that pair removes created isolation. Before the release
     the owner runs it instead. Both orders are pinned in 1.2.14 and 1.6.7.
- 2026-09-27: Plan Adversary consensus (gobby#14579) on `4130a4042f`. PAL-14,
  PAL-15 and PAL-16 are resolved, and the settlement-retry amendment has no
  outstanding blocking findings. The Adversary derives and applies M1 from the
  committed bytes and validates expansion from them. The Program Director
  reviews the plan and gates expansion on Josh's approval. No expansion or
  dispatch happens before that approval.
- 2026-09-28: Reviewer 4 (gobby#14681) bounced 3.1, and the PD accepted the
  bounce with a narrow repair. 3.1 becomes an isolated two-seat placement
  acceptance fixture: `tests/workflows/fixtures/two_seat_placement.yaml`,
  imported only by the isolated test runtime, with no bundled template and
  no live-registry publication. It proves placement-level refusal only
  (`seat_live` is keyed by workspace and title). The V1 smoke imports the
  fixture into an isolated scratch daemon and state, and the operator-facing
  guarded runbook and its smoke are #22895's. The test module is renamed
  `test_placed_pipeline_fixture.py`. Acceptance 3.1.1 to 3.1.5, the
  two-seat placement assertions, the failure behavior, and P1, P2 and P4
  are unchanged. No dependency on #22895 is added. The superseded M1 is
  removed, and the Adversary rederives it after consensus.
- 2026-09-28: Plan Adversary consensus (gobby#14579) on `65f42c93e8`. P1, P2
  and P4 are byte-identical to `a7f3798a3d`. 3.1 is a test-owned fixture
  with no bundled or live publication, and it proves placement-level refusal
  with the roster runbook left to #22895. The V1 smoke runs in an isolated
  scratch daemon, and acceptance 3.1.1 to 3.1.5 are preserved. No findings
  remain open, base validation passes, and no implementation tests were run.
  The Adversary derives and applies a fresh M1 from these bytes. The PD's
  review and Josh's approval gate expansion of the changed artifact.

## M1 Task Manifest
`kind: manifest`

```yaml
- title: Agent pane reservation primitives
  category: code
  task_type: feature
  depends_on:
  - '1.5'
  - '1.6'
  validation_criteria: '1.1.1: Invalid placement shapes (no title, bad axis, both
    variants, unknown key) raise `invalid_placement` without touching storage. test:
    `tests/terminals/test_workspace_agent_panes.py::test_invalid_placement_shapes_refused`.

    1.1.2: Preflight refuses an unknown ref, an out-of-scope project and a foreign
    node, and inserts no row. test: `tests/terminals/test_workspace_agent_panes.py::test_preflight_refusals_have_no_side_effects`.

    1.1.3: Preflight refuses `seat_live` when a tab title or pane label with the same
    canonical title holds a pending, live or orphaned terminal, across kinds in both
    directions (an existing tab then a split request, an existing split then a tab
    request). Two titles that truncate to the same stored value are one seat, and
    relaunch is allowed once that terminal has ended. test: `tests/terminals/test_workspace_agent_panes.py::test_live_seat_refused_across_kinds_ended_seat_allowed`.

    1.1.4: Reserve then bind produces a bound pane with `owns_terminal=True`, keeps
    the in-flight mark until `settle`, and emits exactly one `tab.created` or `pane.added`.
    test: `tests/terminals/test_workspace_agent_panes.py::test_reserve_bind_emits_once`.

    1.1.5: With no launch terminal, or an inactive one, release removes the reserved
    pane and the tab it emptied, and is idempotent. test: `tests/terminals/test_workspace_agent_panes.py::test_release_is_idempotent`.

    1.1.6: Two concurrent reservations of the same workspace and canonical title yield
    exactly one reservation and one `seat_live` refusal, for same-kind and cross-kind
    pairs. test: `tests/terminals/test_workspace_agent_panes.py::test_concurrent_same_seat_reserves_once`.

    1.1.7: A `right` split is stored with axis `horizontal`, and a `down` split with
    axis `vertical`. test: `tests/terminals/test_workspace_agent_panes.py::test_split_axis_maps_to_storage_axis`.

    1.1.8: A tab reservation stores the `worktree_id` passed to `reserve`: none for
    isolation none, the reused id for a reused worktree, and the new id for a fresh
    worktree. test: `tests/terminals/test_workspace_agent_panes.py::test_reserve_stores_final_worktree_association`.

    1.1.9: A `create_tab` or `add_pane` failure, and a `rename_pane` failure after
    `add_pane` committed, each leave no pane row, tab row, in-flight mark or seat
    entry, and a later reserve of the same seat succeeds. test: `tests/terminals/test_workspace_agent_panes.py::test_reserve_insert_failure_leaves_nothing`.

    1.1.10: A reserve cancelled while its insert is running waits for the insert to
    settle, removes any committed row, clears the mark and the seat entry, and re-raises
    `CancelledError`. A later reserve of the same seat succeeds. test: `tests/terminals/test_workspace_agent_panes.py::test_reserve_cancelled_before_return_leaves_nothing`.

    1.1.11: Release kills an owned terminal that is still `pending` or `live` and
    then removes the pane, kills nothing when that terminal is already inactive, and
    when the kill fails, or the terminal is already `orphaned`, keeps the pane bound
    to that terminal, `orphaned` after a failed kill of a `live` row and still `pending`
    after a failed kill of a `pending` row (1.6), so a same-seat preflight is `seat_live`.
    After that terminal settles `exited`, `sweep_dead_panes` removes the pane and
    the seat is free. test: `tests/terminals/test_workspace_agent_panes.py::test_release_kills_only_an_active_owned_terminal`.

    1.1.12: With an active owned terminal, release still kills it and clears the mark
    and the seat entry when the kill raises, when `remove_pane` raises, when the removal
    publish raises, and when the row is already gone. Each failure is logged with
    the phase, the pane and terminal ids and the exception type name, a synthetic
    secret in the exception message is absent from the log, release does not raise,
    and after a successful cleanup has marked the terminal inactive release issues
    no kill. test: `tests/terminals/test_workspace_agent_panes.py::test_release_steps_are_independent`.

    1.1.13: When the rollback''s `remove_pane` fails, reserve logs that failure with
    the pane id and the exception type name and without the exception message (a synthetic
    secret marker is absent from the log), clears the mark and the seat entry, and
    raises the original error. The residual unbound row is not a live seat, a same-seat
    reserve succeeds, and the next `sweep_dead_panes` removes the row. test: `tests/terminals/test_workspace_agent_panes.py::test_reserve_rollback_failure_leaves_sweepable_residue`.

    1.1.14: `settle` clears the mark and the seat entry after a successful reply,
    and until then a guarded close, move or swap of the bound pane is refused `busy`.
    test: `tests/terminals/test_workspace_agent_panes.py::test_mark_held_until_settle`.

    1.1.15: A split whose beside pane, or that pane''s tab, moved to another tab,
    workspace or project after preflight is refused `not_found` at reserve and inserts
    nothing. test: `tests/terminals/test_workspace_agent_panes.py::test_split_reserve_refuses_moved_target`.'
  labels:
  - covers:placed-agent-launch:1.1:1.1.1
  - covers:placed-agent-launch:1.1:1.1.2
  - covers:placed-agent-launch:1.1:1.1.3
  - covers:placed-agent-launch:1.1:1.1.4
  - covers:placed-agent-launch:1.1:1.1.5
  - covers:placed-agent-launch:1.1:1.1.6
  - covers:placed-agent-launch:1.1:1.1.7
  - covers:placed-agent-launch:1.1:1.1.8
  - covers:placed-agent-launch:1.1:1.1.9
  - covers:placed-agent-launch:1.1:1.1.10
  - covers:placed-agent-launch:1.1:1.1.11
  - covers:placed-agent-launch:1.1:1.1.12
  - covers:placed-agent-launch:1.1:1.1.13
  - covers:placed-agent-launch:1.1:1.1.14
  - covers:placed-agent-launch:1.1:1.1.15
  tdd: true
  source_section: '1.1'
  implementation_domain: backend
- title: Executor binds a placed terminal before exec
  category: code
  task_type: feature
  depends_on:
  - '1.6'
  - '1.9'
  validation_criteria: '1.2.1: With a binder, the executor runs `wrap_provider_command`,
    `create_pending`, the bind, `reserve_observer`/`prepare_spawn` and provider exec
    in that order, and the provider argv is the SRT-wrapped command. Without a binder
    the order is unchanged. test: `tests/agents/test_spawn_executor_placement_bind.py::test_bind_follows_wrap_and_precedes_exec`.

    1.2.2: A binder failure, including a publish failure raised after `set_pane_terminal`
    persisted the binding, fails the pending terminal through `_settle_native_spawn_failure`,
    returns a failed `SpawnResult`, and never starts the provider. test: `tests/agents/test_spawn_executor_placement_bind.py::test_bind_failure_fails_pending_terminal`.

    1.2.3: `spawn_executor` re-exports `_runtime_spawn` and `_promote_prepared` from
    `spawn_executor_runtime`. symbol: `_runtime_spawn`. file: `src/gobby/agents/spawn_executor_runtime.py`.

    1.2.4: With a binder, a `timeout_seconds` expiry hands the held claim to the owner,
    the row stays `pending` while the prepare is unresolved, and `spawn_timeout` returns
    without awaiting the prepare. A late success with a proven kill settles the row
    `exited`. After a late success, a tmux kill that leaves the session present, a
    tmux terminate that raises, and a native stale-epoch kill whose host cannot answer
    and that has no process each move the row from `pending` to `orphaned` in one
    CAS carrying the prepared locator, locator key, epoch and process. A native stale-epoch
    kill with a process whose group is then dead settles `exited`. When the final
    settlement write succeeds, the claim is released in every case, for native and
    tmux. test: `tests/agents/test_spawn_executor_placement_bind.py::test_placed_timeout_holds_pending_then_late_settlement`.

    1.2.5: Without a binder, a timeout keeps today''s pending row and late cleanup.
    test: `tests/agents/test_spawn_executor_placement_bind.py::test_unplaced_timeout_unchanged`.

    1.2.6: With a binder, repeated cancellation while the prepare is unresolved, and
    a cancellation that arrives as the prepare completes with either a success or
    a failure, each hand the prepare to exactly one owner, which consumes it, and
    return `cancelled`. test: `tests/agents/test_spawn_executor_placement_bind.py::test_placed_cancellation_has_one_owner`.

    1.2.7: The claim is taken before `create_pending` is dispatched and released after
    a successful promote or a bind failure. While it is held, `cleanup_failed_spawn`
    and `terminal_kill` each report not settled, leave the unresolved row `pending`
    and start no runtime terminate. A prepare slower than `spawn_in_doubt_seconds`
    is reaped by neither reaper. test: `tests/agents/test_spawn_executor_placement_bind.py::test_in_doubt_claim_spans_prepare`.

    1.2.8: A late prepare failure is not proof. A tmux session created before a failing
    dimension query, and a native terminal created on the host whose spawn response
    was lost, are each found and killed with proof before the row settles `exited`.
    A native probe that cannot reach the host leaves the row `pending` with the claim
    held, and the absence retry settles it once the host answers (1.2.12). test: `tests/agents/test_spawn_executor_placement_bind.py::test_late_prepare_failure_requires_proven_absence`.

    1.2.9: With a binder, each exit E1-E14 either releases the claim inline or hands
    it to exactly one owner, and none leaves the id held without an owner. Each case
    is one parametrized row. Cancellation cases cancel the caller repeatedly while
    the stage''s worker is still running, and the owner awaits that worker''s real
    completion. A `create_pending` whose row commits after the cancellation gets that
    row settled by the owner. The E12 cases (observer bind failure, `CommitSpawnRefusedError`,
    native commit error, lost CAS), with a kill that raises or is swallowed, leave
    the row unterminalized until the owner''s proof, and the owner kills through the
    `prepared` identity. test: `tests/agents/test_spawn_executor_placement_bind.py::test_every_exit_releases_or_hands_off_the_claim`.

    1.2.10: An injected failure of `mark_kill_failed` still lets the owner consume
    the prepare and make its kill decision. A final settlement write that fails after
    three retries leaves the claim held and the row unsettled, and the owner enters
    its settlement retry. With `mark_kill_failed` failing on every attempt and the
    kill unproven, a `record_orphan_identity` CAS that matches no row keeps the claim
    held. A final `exited` or `fail_pending_attempt` read back in the wrong state
    also keeps the claim, and an already-`exited` row releases. On a timeout the row
    stays `pending` under the held claim while the prepare is still unresolved. test:
    `tests/agents/test_spawn_executor_placement_bind.py::test_owner_contains_storage_failures`.

    1.2.11: With storage failing past the three immediate retries, the owner keeps
    the claim and retries on the capped backoff with no restart. Once storage recovers,
    it settles and releases exactly once for each settlement: `exited` after a proven
    kill, a kept orphan (from `pending` in one `mark_kill_failed` CAS with identity,
    and from `live` by `mark_kill_failed` then `record_orphan_identity`), and `fail_pending_attempt`
    at stages `create`, `bind` and `reserve`. Every re-issued write carries the captured
    `attempt_generation` and `attempt_started_at`, and a read-back with another pair
    is not confirmation. A CAS that first matches no row and a wrong-state read-back
    each retry and then settle. Compensation runs once for `exited` and for `fail_pending_attempt`
    and is dropped for a kept orphan. For `create`, `bind` and `reserve`, a created-isolation
    step deferred before the release runs once after the confirmed settlement, and
    one deferred after the release decides from the row: `exited`, no row, or a row
    still carrying the result''s `prior_attempt` pair removes, and any other state
    keeps. No settlement cycle calls terminate, the host probe, the prepare or provider
    code. While the owner retries, `terminal_kill`, reconcile and both stale-pending
    reapers leave the row unchanged, and a second claim on the id is refused. test:
    `tests/agents/test_spawn_executor_placement_bind.py::test_owner_retries_settlement_until_storage_recovers`.

    1.2.12: A prepare that fails with absence unproven, for native (`find_host_terminal`
    raises) and tmux (`session_present` raises, or the session stays present after
    an unproven kill), leaves the row `pending`, never `orphaned`, with the claim
    held. On the capped backoff the owner re-runs the presence check and, while the
    session is present, re-issues the strict kill at most once per cycle; it never
    re-dispatches the prepare or runs provider code. Once the check proves absence
    or a kill is proven, it settles `fail_pending_attempt`, releases and runs compensation
    once, with no restart. A prepare that succeeded followed by an unproven kill moves
    the `pending` row to `orphaned` with the prepared identity in one CAS. After a
    simulated restart such a `pending` row settles only on proof: `LifecycleReconciliation.reap_stale_pending`
    delegates to the strict reaper, a tmux row whose session is present and a native
    row the host lists or cannot answer for stay `pending`, and a native row absent
    from the strict listing settles `exited`. test: `tests/agents/test_spawn_executor_placement_bind.py::test_identityless_row_stays_pending_until_absence_proven`.

    1.2.13: Repeatedly cancelling the spawn caller and a waiter that awaits the owner
    through `asyncio.shield` does not stop the owner. The cancellations land during
    the stage drain, the first settlement, a backoff sleep and a shielded offloaded
    settlement write. When storage then recovers with no restart, there is one owner
    task, one confirmed settlement, one `release` and one compensation run. In a separate
    shutdown case, cancelling the owner task itself as loop teardown does, at each
    of those points, releases nothing, runs no deferred step, leaves the claim until
    process exit and leaves the row for restart recovery. test: `tests/agents/test_spawn_executor_placement_bind.py::test_owner_retry_survives_cancellation_until_shutdown`.

    1.2.14: A raised `create_pending` or bump is recovered by read-back and never
    re-executed. A create that commits and then raises, and a bump that commits and
    then raises, each leave a `pending` row that the owner adopts by its pair and
    settles `fail_pending_attempt`. A create that rolls back leaves no row, and a
    bump that rolls back leaves the pre-existing attempt unchanged; each settles with
    no write. With the read-back failing during a storage outage, the claim stays
    held and the owner recovers and settles once storage returns, with no restart.
    Compensation runs once in every case. For a rolled-back bump whose earlier attempt''s
    row is `pending`, a created-isolation step deferred before the release is run
    once by the owner. One deferred after the release sees the row still carrying
    the result''s `prior_attempt` and removes created isolation. A row carrying a
    newer pair keeps it, and reused isolation is never registered or removed. test:
    `tests/agents/test_spawn_executor_placement_bind.py::test_indeterminate_create_is_recovered_by_read_back`.'
  labels:
  - covers:placed-agent-launch:1.2:1.2.1
  - covers:placed-agent-launch:1.2:1.2.2
  - covers:placed-agent-launch:1.2:1.2.3
  - covers:placed-agent-launch:1.2:1.2.4
  - covers:placed-agent-launch:1.2:1.2.5
  - covers:placed-agent-launch:1.2:1.2.6
  - covers:placed-agent-launch:1.2:1.2.7
  - covers:placed-agent-launch:1.2:1.2.8
  - covers:placed-agent-launch:1.2:1.2.9
  - covers:placed-agent-launch:1.2:1.2.10
  - covers:placed-agent-launch:1.2:1.2.11
  - covers:placed-agent-launch:1.2:1.2.12
  - covers:placed-agent-launch:1.2:1.2.13
  - covers:placed-agent-launch:1.2:1.2.14
  tdd: true
  source_section: '1.2'
  implementation_domain: backend
- title: One daemon-scoped reserver reaches spawn_agent
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  validation_criteria: '1.3.1: `configure_terminals` builds exactly one `AgentPaneReserver`
    with the same workspace manager, terminal manager, runtime registry, sessions
    and publish callback as `WorkspaceOps`, and builds none when `WorkspaceOps` is
    not built. test: `tests/terminals/test_composition_roots.py::test_configure_terminals_builds_one_agent_pane_reserver`.

    1.3.2: `register_agent_spawn_tools` hands the context''s resolver to `create_spawn_agent_registry`
    unchanged, and each resolution returns the server''s single reserver. test: `tests/mcp_proxy/tools/test_agents_spawn_tools.py::test_spawn_registry_resolves_one_daemon_reserver`.'
  labels:
  - covers:placed-agent-launch:1.3:1.3.1
  - covers:placed-agent-launch:1.3:1.3.2
  tdd: true
  source_section: '1.3'
  implementation_domain: backend
- title: spawn_agent placement input, compensation and reply
  category: code
  task_type: feature
  depends_on:
  - '1.1'
  - '1.2'
  - '1.3'
  - '1.6'
  validation_criteria: '1.4.1: A refused preflight (`invalid_placement`, `seat_live`,
    `not_found`, `forbidden`) creates no isolation, no child session, no agent run,
    no terminal and no pane. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_refused_placement_has_no_side_effects`.

    1.4.2: A placed spawn with a sandbox config that is not SRT, or not enabled, is
    refused with `sandbox_required` before any side effect. Leaf-local: until 1.8
    lands, unplaced spawns are unaffected; 1.8 extends the refusal to every launch.
    test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_placed_launch_requires_managed_srt`.

    1.4.3: A provider or SRT preparation failure that `finalize_executed_spawn` returns
    as `success: false` releases the pane, runs `cleanup_failed_spawn` exactly once,
    and never starts the provider. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_wrap_failure_refuses_and_releases_pane`.

    1.4.4: A later returned failure (terminal liveness or start-run) releases the
    pane, kills the bound terminal, and runs `cleanup_failed_spawn` exactly once.
    test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_late_returned_failure_releases_pane_once`.

    1.4.5: An exception, a `CancelledError` and a bind `busy` conflict each release
    the pane, and release still runs when a `cleanup_failed_spawn` step fails. When
    cleanup''s terminate step did not reach the terminal, release kills the recorded
    launch terminal, or marks it orphaned when the kill fails. After a `busy` conflict
    release kills nothing. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_exceptions_and_cancellation_release_pane`.

    1.4.6: A successful placed spawn returns synchronously with run_id, terminal_id,
    workspace, tab_ref and pane_ref, and keeps its pane bound. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_placed_spawn_reply_carries_refs`.

    1.4.7: A slot, lease or active-task refusal leaves no pane, and the loser of a
    reserve-time `seat_live` race cleans its run, child session and created isolation.
    test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_late_refusals_leave_no_pane`.

    1.4.8: An explicit `project_path` is accepted. A resolved parent is accepted,
    including a pipeline child parented to the system or cron session. An ambient-only
    project and a parent that is the system session itself are refused with `parent_unresolved`
    before any side effect. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_parent_and_project_provenance`.

    1.4.9: A placed spawn that fails after creating its own worktree removes that
    worktree, and one that reused a `worktree_id` keeps it. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_failed_placed_spawn_cleans_created_isolation_only`.

    1.4.10: Two concurrent placed spawns of one seat through the registry yield one
    placed agent and one `seat_live`, and the winner''s bind publishes `tab.created`
    or `pane.added` through the server''s workspace broadcast before provider exec.
    test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_concurrent_placed_spawns_share_one_reserver`.

    1.4.11: A `reserve` that raises, and a cancellation that arrives while `reserve`
    is running, each run `_spawn_failure` once, which removes the run, the child session
    and created isolation, and leave no pane, tab or in-flight mark. The cancellation
    is re-raised. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_reserve_failure_and_cancellation_clean_dispatch_state`.

    1.4.12: When bind''s publish raises after `set_pane_terminal` persisted the binding,
    release still receives the launch terminal id. When the pending-failure settlement
    fails and `cleanup_failed_spawn`''s terminate step also fails before that terminal
    is inactive, release removes the pane and kills the terminal exactly once. When
    cleanup completes instead, release issues no kill. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_bind_publish_failure_keeps_release_kill_backstop`.

    1.4.13: Through the registry, a placed request for a free seat whose task already
    has an active run is refused `task_active` with that run id and removes the isolation
    it created. A placed request for the occupied seat is `seat_live`. An unplaced
    duplicate keeps the skipped reply, and no second agent starts. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_duplicate_placed_request_precedence`.

    1.4.14: A placed spawn whose `timeout_seconds` expires while the prepare is unresolved
    runs finalize, `cleanup_failed_spawn`, release and then a `pane_close` of the
    bound pane with the claim held. The row stays `pending`, the `pane_close` is refused
    `busy`, the pane stays bound and holds the seat, created isolation still exists,
    and nothing is marked `exited`. When the late prepare succeeds, the owner''s proven
    kill settles the row `exited` and removes the created worktree, and `sweep_dead_panes`
    then frees the pane. On an unproven kill the worktree and the pane are kept. This
    holds for native and tmux. test: `tests/mcp_proxy/tools/spawn_agent/test_placement.py::test_placed_timeout_race_keeps_pane_until_owner_settles`.'
  labels:
  - covers:placed-agent-launch:1.4:1.4.1
  - covers:placed-agent-launch:1.4:1.4.2
  - covers:placed-agent-launch:1.4:1.4.3
  - covers:placed-agent-launch:1.4:1.4.4
  - covers:placed-agent-launch:1.4:1.4.5
  - covers:placed-agent-launch:1.4:1.4.6
  - covers:placed-agent-launch:1.4:1.4.7
  - covers:placed-agent-launch:1.4:1.4.8
  - covers:placed-agent-launch:1.4:1.4.9
  - covers:placed-agent-launch:1.4:1.4.10
  - covers:placed-agent-launch:1.4:1.4.11
  - covers:placed-agent-launch:1.4:1.4.12
  - covers:placed-agent-launch:1.4:1.4.13
  - covers:placed-agent-launch:1.4:1.4.14
  tdd: true
  source_section: '1.4'
  implementation_domain: backend
- title: Workspace mutations refuse in-flight panes atomically
  category: code
  task_type: bug
  depends_on:
  - '1.6'
  validation_criteria: '1.5.1: Each guarded mutation (`close`, `close_tab`, `remove_pane`,
    `move_pane`, `move_tab`, `swap_panes`) raises `WorkspaceBusyError` for an in-flight
    pane, bound or unbound, and for a pane whose bound terminal is held in-doubt after
    its mark was cleared, and changes nothing. test: `tests/storage/test_workspaces.py::test_guarded_mutations_refuse_in_flight_panes`.

    1.5.2: A guarded mutation racing an agent-pane insert in either order either refuses
    busy or runs against the committed pane; neither commits against a stale read.
    test: `tests/storage/test_workspaces.py::test_guard_and_insert_serialize`.

    1.5.3: `add_pane` with expected ids refuses a beside pane moved to another tab,
    a tab moved to another workspace, and a tab of another project in the same workspace,
    and inserts nothing. test: `tests/storage/test_workspaces.py::test_add_pane_refuses_moved_beside_target`.

    1.5.4: An empty-workspace close and a concurrent `create_tab` serialize: either
    the close wins and the insert fails not found, or the insert wins and the close
    refuses busy. test: `tests/storage/test_workspaces.py::test_empty_workspace_close_serializes_with_new_tab`.

    1.5.5: `sweep_dead_panes` keeps a pane whose terminal is `orphaned` and removes
    it once the terminal is `exited`. test: `tests/storage/test_workspaces.py::test_sweep_keeps_orphaned_panes`.

    1.5.6: `WorkspaceOps` close, move and swap return `busy` for an in-flight pane,
    and a `pane_close` on an `orphaned` pane retries its kill. test: `tests/terminals/test_workspace_ops.py::test_ops_refuse_in_flight_and_retry_orphaned_kill`.

    1.5.7: `storage/workspaces.py` is under 1,000 lines and `workspace_ops.py` under
    850, and the moved names import from their new modules. file: `src/gobby/storage/workspace_layout.py`.

    1.5.8: The pane I/O methods live in `WorkspacePaneIOMixin`, `WorkspaceOps` inherits
    them with unchanged signatures, and the existing pane I/O tests pass unchanged
    apart from the retargeted clock patches. file: `src/gobby/terminals/workspace_pane_io.py`.

    1.5.9: A `workspace_close` or `tab_close` whose pane read precedes a reservation
    that inserts, binds and settles a pane before the close''s transaction runs is
    refused `busy`, deletes nothing and kills nothing. test: `tests/terminals/test_workspace_ops.py::test_close_refuses_membership_drift_since_read`.'
  labels:
  - covers:placed-agent-launch:1.5:1.5.1
  - covers:placed-agent-launch:1.5:1.5.2
  - covers:placed-agent-launch:1.5:1.5.3
  - covers:placed-agent-launch:1.5:1.5.4
  - covers:placed-agent-launch:1.5:1.5.5
  - covers:placed-agent-launch:1.5:1.5.6
  - covers:placed-agent-launch:1.5:1.5.7
  - covers:placed-agent-launch:1.5:1.5.8
  - covers:placed-agent-launch:1.5:1.5.9
  tdd: true
  source_section: '1.5'
  implementation_domain: backend
- title: 'Spawn failure cleanup: one attempt, cancellation-safe, kill truth'
  category: code
  task_type: bug
  depends_on:
  - '1.9'
  validation_criteria: '1.6.1: `mark_kill_failed` moves a `live` row to `orphaned`,
    moves a `pending` row to `orphaned` only with a full identity written in the same
    statement (which passes the schema CHECKs), leaves a `pending` row without identity
    `pending`, and leaves other states, and a row with another attempt pair, unchanged.
    test: `tests/storage/test_terminal_kill_settlement.py::test_mark_kill_failed_cas`.

    1.6.2: A failed runtime kill of a native pid-less `pending` terminal, a tmux `pending`
    terminal whose session is still present after terminate, and a held in-doubt id
    each leave the row `pending` and keep created isolation, and a failed kill of
    a `live` terminal leaves it `orphaned` and listed; a successful kill settles it
    and cleanup removes created isolation. test: `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::test_failed_kill_orphans_and_keeps_isolation`.

    1.6.3: A failure inside `finalize_executed_spawn`''s cleanup followed by `_spawn_failure`
    runs cleanup once. test: `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::test_cleanup_runs_once_per_attempt`.

    1.6.4: A cancellation during cleanup, and a second cancellation during a later
    step, let every step finish once and re-raise the first cancellation; a cancellation
    after cleanup settled starts nothing. test: `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::test_cleanup_survives_cancellation`.

    1.6.5: With each step failing in turn, every later step still runs, each failure
    is logged with the phase, the run id and the exception type name, a synthetic
    secret in the exception message is absent from the log, and cleanup does not raise.
    test: `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::test_cleanup_steps_are_independent`.

    1.6.7: With the terminal held in-doubt, cleanup registers created-isolation removal
    with the claim and removes nothing itself, and reused isolation is not registered.
    After the claim ends, cleanup removes created isolation only for an `exited` row,
    no row, or a row still carrying the failed result''s `prior_attempt` pair; a `pending`,
    `live` or `orphaned` row with any other pair keeps it. test: `tests/mcp_proxy/tools/spawn_agent/test_failure_cleanup.py::test_held_terminal_defers_isolation_to_owner`.

    1.6.6: Every production `cleanup_failed_spawn` call passes the attempt''s `SpawnCleanupOnce`.
    symbol: `SpawnCleanupOnce`. file: `src/gobby/mcp_proxy/tools/spawn_agent/_failure_cleanup.py`.'
  labels:
  - covers:placed-agent-launch:1.6:1.6.1
  - covers:placed-agent-launch:1.6:1.6.2
  - covers:placed-agent-launch:1.6:1.6.3
  - covers:placed-agent-launch:1.6:1.6.4
  - covers:placed-agent-launch:1.6:1.6.5
  - covers:placed-agent-launch:1.6:1.6.7
  - covers:placed-agent-launch:1.6:1.6.6
  tdd: true
  source_section: '1.6'
  implementation_domain: backend
- title: Placed resume re-places against current state
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  validation_criteria: '1.7.1: A placed agent''s resume snapshot carries its validated
    placement, and an unplaced agent''s carries none. test: `tests/agents/test_resume_placement.py::test_snapshot_carries_validated_placement`.

    1.7.2: A placed resume re-runs preflight, reserve and bind before exec, and on
    success the pane is bound to the new terminal and settled. test: `tests/agents/test_resume_placement.py::test_placed_resume_replaces_before_exec`.

    1.7.3: A moved or missing target, an occupied seat (including `orphaned`), a forbidden
    project, a wrap failure and a missing reserver each park the successor, return
    a typed error, start no provider and leave no pane. test: `tests/agents/test_resume_placement.py::test_placed_resume_refusals_park_successor`.

    1.7.4: A placed resume failure or cancellation runs cleanup and release once each
    and never falls back to an unplaced launch. test: `tests/agents/test_resume_placement.py::test_placed_resume_cleanup_once`.

    1.7.5: `resume_executor.py` is under 1,000 lines and the moved helpers import
    from `resume_executor_settlement.py`. file: `src/gobby/agents/resume_executor_settlement.py`.

    1.7.6: A placed resume cancelled repeatedly while its prepare is unresolved hands
    the prepare to the 1.2 owner and keeps the pane bound to the `pending` row. The
    owner''s late settlement then lets the pane be freed, and no unplaced fallback
    runs. test: `tests/agents/test_resume_placement.py::test_placed_resume_cancel_keeps_in_doubt_owner`.'
  labels:
  - covers:placed-agent-launch:1.7:1.7.1
  - covers:placed-agent-launch:1.7:1.7.2
  - covers:placed-agent-launch:1.7:1.7.3
  - covers:placed-agent-launch:1.7:1.7.4
  - covers:placed-agent-launch:1.7:1.7.5
  - covers:placed-agent-launch:1.7:1.7.6
  tdd: true
  source_section: '1.7'
  implementation_domain: backend
- title: spawn_agent and resume never launch unsandboxed
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  - '1.6'
  - '1.7'
  validation_criteria: '1.8.1: An unplaced spawn whose effective config is `enabled:
    false`, or whose backend is `provider-native`, is refused `sandbox_required` before
    isolation, a child session or a run exists. test: `tests/mcp_proxy/tools/spawn_agent/test_sandbox_gate.py::test_unsandboxed_config_refused_before_side_effects`.

    1.8.2: A managed runtime profile config goes through the same gate. test: `tests/mcp_proxy/tools/spawn_agent/test_sandbox_gate.py::test_runtime_profile_config_is_gated`.

    1.8.3: A resume with no snapshot config, `enabled: false` or a non-`srt` backend
    parks the successor, returns `sandbox_required` and starts no provider. test:
    `tests/agents/test_resume_sandbox_gate.py::test_resume_refuses_unsandboxed_config`.

    1.8.5: From a spawned agent, a REST or CLI MCP call is rule-enforced. test: `tests/mcp_proxy/tools/spawn_agent/test_sandbox_gate.py::test_loopback_mcp_calls_are_rule_enforced`.

    1.8.6: With no verifier patch and `GOBBY_HOME` set to an empty directory, the
    real `verify_srt_installation` fails and the gate refuses the spawn `sandbox_required`;
    gate unit cases that patch the verifier assert both outcomes explicitly. test:
    `tests/mcp_proxy/tools/spawn_agent/test_sandbox_gate.py::test_gate_refuses_when_isolated_srt_is_missing`.'
  labels:
  - covers:placed-agent-launch:1.8:1.8.1
  - covers:placed-agent-launch:1.8:1.8.2
  - covers:placed-agent-launch:1.8:1.8.3
  - covers:placed-agent-launch:1.8:1.8.5
  - covers:placed-agent-launch:1.8:1.8.6
  tdd: true
  source_section: '1.8'
  implementation_domain: backend
- title: In-doubt spawn ownership, kill truth and restart recovery
  category: code
  task_type: bug
  depends_on: []
  validation_criteria: '1.9.1: `claim`, `holds`, `defer` and `release` track one id;
    `defer` on an unheld id returns false; `release` returns the deferred steps once,
    and a `defer` after it returns false. `holds` called from a worker thread while
    the loop claims and releases answers consistently. test: `tests/terminals/test_in_doubt_kill_truth.py::test_in_doubt_registry_claims_defers_and_releases`.

    1.9.2: `kill_terminal` on a held id raises `TerminalInDoubtError`, calls no runtime
    terminate and leaves the row unchanged. After `release`, the same call kills the
    terminal and marks it `exited`. test: `tests/terminals/test_in_doubt_kill_truth.py::test_kill_terminal_refuses_held_ids`.

    1.9.3: A tmux terminate that leaves the session present makes `kill_terminal`
    raise `TerminalKillUnprovenError` and leave the row unsettled. Before any reconcile,
    a native row with a stale non-NULL epoch that the current host still lists is
    killed through the host id the strict probe returns before it is marked `exited`.
    The same row absent from the listing is reaped and settles `exited` (#22530).
    With the host unreachable, it settles only when a usable recorded process (a positive
    integer `pgid`) has a dead group afterwards. A live group, no process, a host
    id with no process, and a non-integer or non-positive `pgid` each stay unsettled.
    A `pending` row follows the same three cases, and a current-epoch row is not treated
    as stale while the client is unconnected. A `kill_terminal` given a stale `Terminal`
    decides from the row it reread under `settle_lock`. test: `tests/terminals/test_in_doubt_kill_truth.py::test_kill_terminal_requires_proven_kill`.

    1.9.4: `record_orphan_identity` writes the locator, epoch and process on an `orphaned`
    row and changes no row in any other state. test: `tests/terminals/test_in_doubt_kill_truth.py::test_record_orphan_identity_cas`.

    1.9.5: After a simulated restart, an `orphaned` row whose recorded host identity
    is stale and that the host lists gets the current host identity and process from
    reconcile, and `terminal_kill` then calls the host kill before it marks the row
    `exited`. A listed row that reconcile finds held, or whose state or attempt pair
    changed under the lock, is untouched. `mark_exited_attempt` and `record_orphan_identity`
    change no row with another attempt pair. test: `tests/terminals/test_host_reconcile_orphans.py::test_reconcile_recovers_orphan_identity`.

    1.9.6: `find_host_terminal` returns the matching host id or `None`, and raises
    when the host is unreachable. `LifecycleReconciliation.reap_stale_pending` skips
    a held `pending` row older than `spawn_in_doubt_seconds`. test: `tests/terminals/test_in_doubt_kill_truth.py::test_probe_is_strict_and_reaper_honors_claims`.'
  labels:
  - covers:placed-agent-launch:1.9:1.9.1
  - covers:placed-agent-launch:1.9:1.9.2
  - covers:placed-agent-launch:1.9:1.9.3
  - covers:placed-agent-launch:1.9:1.9.4
  - covers:placed-agent-launch:1.9:1.9.5
  - covers:placed-agent-launch:1.9:1.9.6
  tdd: true
  source_section: '1.9'
  implementation_domain: backend
- title: gclient reconciliation of daemon-placed terminals
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  validation_criteria: '2.1.1: A binding workspace event that arrives before `created`
    means the terminal opens only in its bound daemon pane, with no local surface
    in the final Chrome state. test: `crates/gclient/tests/placed_agent.rs::bind_then_created_opens_once_in_pane`.

    2.1.2: A `created` event that arrives before the binding workspace event gets
    its unplaced surface moved into the bound daemon pane, leaving exactly one surface
    and no local tab. test: `crates/gclient/tests/placed_agent.rs::created_then_bind_moves_into_pane`.

    2.1.3: A client that starts or reconnects through `reconcile_subscribe_first`,
    with a snapshot holding the bound pane and a roster holding the already-live terminal,
    shows exactly one surface in the saved pane, no local surface, and stable focus.
    test: `crates/gclient/tests/placed_agent.rs::reconnect_projects_bound_terminal_once`.'
  labels:
  - covers:placed-agent-launch:2.1:2.1.1
  - covers:placed-agent-launch:2.1:2.1.2
  - covers:placed-agent-launch:2.1:2.1.3
  tdd: true
  source_section: '2.1'
  implementation_domain: frontend
- title: Two-seat placement acceptance fixture
  category: code
  task_type: feature
  depends_on:
  - '1.4'
  - '2.1'
  validation_criteria: '3.1.1: The two-seat pipeline places seat A in a new titled
    tab and seat B in a right split of seat A''s pane, stored with axis `horizontal`.
    Both terminals are SRT-wrapped and bound before exec, and both replies carry pane
    refs. test: `tests/workflows/test_placed_pipeline_fixture.py::test_two_seat_tab_and_split`.

    3.1.2: Re-running the pipeline while seat A is live fails at `seat_a` with `seat_live`
    and spawns nothing. test: `tests/workflows/test_placed_pipeline_fixture.py::test_rerun_refuses_live_seat`.

    3.1.3: An invalid pane ref for seat B fails the step with no spawn, and seat A
    is unaffected. test: `tests/workflows/test_placed_pipeline_fixture.py::test_invalid_ref_refuses_without_spawn`.

    3.1.4: An SRT wrap failure for seat B fails the step, leaves no seat B pane and
    no provider process, and leaves seat A unaffected. test: `tests/workflows/test_placed_pipeline_fixture.py::test_wrap_failure_refuses_seat`.

    3.1.5: A CLI-started run parents both agents to the pipeline child session, whose
    parent is the system session, and resolves the pipeline''s project. test: `tests/workflows/test_placed_pipeline_fixture.py::test_cli_run_parent_and_project`.'
  labels:
  - covers:placed-agent-launch:3.1:3.1.1
  - covers:placed-agent-launch:3.1:3.1.2
  - covers:placed-agent-launch:3.1:3.1.3
  - covers:placed-agent-launch:3.1:3.1.4
  - covers:placed-agent-launch:3.1:3.1.5
  tdd: true
  source_section: '3.1'
  implementation_domain: backend
- title: Vendored Trusted seed and its reviewed refresh
  category: config
  task_type: feature
  depends_on: []
  validation_criteria: '4.1.1: The vendored seed records its source URL and fetch
    date, its `sha256` matches its domain list, and every entry passes SRT''s domain
    rule. symbol: `trusted_domains`. test: `tests/agents/test_trusted_domains.py::test_seed_provenance_and_entries_are_valid`.

    4.1.2: The refresh script parses the Accordion bullet format, including link and
    escaped entries, rejects an entry SRT would refuse, prints a diff and writes only
    with `--write`. test: `tests/scripts/test_refresh_trusted_domains.py::test_parse_diff_and_write_gate`.'
  labels:
  - covers:placed-agent-launch:4.1:4.1.1
  - covers:placed-agent-launch:4.1:4.1.2
  tdd: true
  source_section: '4.1'
  assigned_agent: backend-developer
- title: Definition network field and sync-only write guard
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: '4.2.1: `AgentDefinitionBody` accepts `network` of `none` or
    `trusted`, defaults to `none`, and rejects any other value; a bundled template''s
    `trusted` survives sync into the stored row. symbol: `AgentDefinitionBody`. test:
    `tests/agents/test_agents_sync.py::TestSyncBundledAgents::test_sync_preserves_trusted_network`.

    4.2.2: Every non-sync write that brings in a body with `network: trusted` is refused:
    create, update, upsert_with_steps and duplicate. test: `tests/storage/definitions/test_agents_manager.py::test_non_sync_writes_refuse_widened_network`.

    4.2.3: A row with `network: trusted` is immutable outside the sync: update, toggle,
    restore, set_step_workflow and both moves refuse. test: `tests/storage/definitions/test_agents_manager.py::test_widened_network_row_is_immutable_outside_sync`.

    4.2.4: The sync creates, updates and restores rows carrying `network: trusted`
    through sync entry points only. test: `tests/agents/test_agents_sync.py::TestSyncBundledAgents::test_sync_new_row_uses_sync_entry_point`.

    4.2.5: An imported global or project agent YAML carrying `network: trusted`, loaded
    through `sync_imported_workflows` (the `reload_cache` path), is refused and leaves
    no row with a widened network. test: `tests/workflows/test_workflows_sync.py::test_imported_agent_yaml_cannot_widen_network`.'
  labels:
  - covers:placed-agent-launch:4.2:4.2.1
  - covers:placed-agent-launch:4.2:4.2.2
  - covers:placed-agent-launch:4.2:4.2.3
  - covers:placed-agent-launch:4.2:4.2.4
  - covers:placed-agent-launch:4.2:4.2.5
  tdd: true
  source_section: '4.2'
  implementation_domain: backend
- title: Resolve the definition's network at spawn
  category: code
  task_type: feature
  depends_on:
  - '1.8'
  - '4.1'
  - '4.2'
  validation_criteria: '4.3.1: A spawned `trusted` definition resolves to enabled
    SRT with `allow_network` false whose domains are the Trusted seed, the git and
    registry groups and the Gobby hosts, and a `none` definition gets exactly the
    `agent_sandbox` policy plus the write grant, placed or not. symbol: `definition_sandbox_config`.
    test: `tests/agents/test_sandbox_network.py::test_trusted_definition_adds_seed_to_srt_allowlist`.

    4.3.2: An unreadable seed refuses a `trusted` spawn with `sandbox_required` before
    any side effect, and a `none` spawn does not read the seed. test: `tests/agents/test_sandbox_network.py::test_unreadable_seed_refuses_trusted_spawn_only`.

    4.3.3: A resumed `trusted` run replays its spawn-time domains through the 1.8
    snapshot path. test: `tests/agents/test_resume_sandbox_gate.py::test_resume_replays_trusted_snapshot`.

    4.3.4: `_implementation.py` does not end above its `c52269ccd5` size. file: `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py`.'
  labels:
  - covers:placed-agent-launch:4.3:4.3.1
  - covers:placed-agent-launch:4.3:4.3.2
  - covers:placed-agent-launch:4.3:4.3.3
  - covers:placed-agent-launch:4.3:4.3.4
  tdd: true
  source_section: '4.3'
  implementation_domain: backend
- title: Document definition network policy in the sandboxing guide
  category: docs
  task_type: chore
  depends_on:
  - '4.3'
  validation_criteria: '4.4.1: The guide documents the `network` field, the seed and
    its refresh, the write guard and the refusal. behavior: "Agent network policy"
    in `docs/guides/sandboxing.md`.'
  labels:
  - covers:placed-agent-launch:4.4:4.4.1
  tdd: false
  source_section: '4.4'
  assigned_agent: tech-writer
```
