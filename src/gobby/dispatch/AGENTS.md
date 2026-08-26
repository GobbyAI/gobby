# Dispatch Architecture

Gobby task automation is stage-manifest dispatch. The dispatcher is a
deterministic heartbeat that scans tasks with `allow_automation=true`, reads the
task's current manifest row, evaluates ordered rules, acquires a per-task mutex,
and executes the selected action.

The dispatch chain is:

```text
task_stages_registry -> task_stage_states manifest -> ordered rule -> action
```

Dispatch rules live in `src/gobby/dispatch/rules.py`. To add a rule:

1. Add a small rule function that checks the current manifest row, artifacts,
   labels, and automation fields.
2. Return an explicit action such as start stage, spawn agent, start expansion,
   create isolation, advance stage, append audit marker, or escalate.
3. Register it in the ordered rule list near the manifest stage it belongs to.
4. Keep the rule deterministic. Prompting belongs in spawned agents; the
   dispatcher only routes manifest state.

Build state is resolved before dispatch:

- `allow_automation` is the opt-in gate. Backlog tasks stay invisible until `gobby build`
  enables them.
- `unattended` means rules choose deterministic fallbacks instead of escalating when possible.
- `isolation` is explicit task state: `none`, `worktree`, or `clone`.
- `stages` is the ordered manifest materialized in `task_stage_states` from the
  stage registry. The current stage is the first row whose state is not `done`.
- Build profiles are DB-backed registry rows synced from
  `src/gobby/install/shared/registry/build_profiles.yaml`. The `default`
  profile resolves unless a caller supplies another profile; explicit CLI,
  MCP, and HTTP build fields override profile defaults for the same fields.
  Profile skip stages only shape a new lifecycle. Existing manifests must be
  cleaned or restarted before their stage shape changes.
- `assigned_agent` and `additional_skills` route leaf work. Missing leaf assignment
  falls back to `backend-developer` with an audit marker.

`gobby build` is the single entry point for turning a plan, epic, or leaf task into
dispatchable state. The CLI command, MCP tool (`gobby-tasks-ops:build_task`), and HTTP
route (`POST /api/build`) must all call the shared build service in
`src/gobby/build/service.py`, returning the same `BuildResult`.
`gobby build stop <ref> [--yes]` is the task-scoped CLI inverse for an
existing built task: it stops automation for the resolved task or subtree
through the shared build control path without deleting task history or build
artifacts. Sibling lifecycle actions on the same command are `resume`,
`clean`, and `restart`.

Concurrency and audit data are adjacent to tasks:

- `task_dispatch_mutex` stores short-lived leases. Dispatcher code is the normal writer:
  acquire before side effects, release in `finally`, sweep expired leases on startup, and
  use the force-release escape hatch only for operator recovery.
- `task_artifacts` stores sparse pointers such as `plan_file_path`, `target_branch`,
  worktree/clone path and ID pairs, expansion run IDs, PR URL, and future merge SHA.
  Write related fields atomically, especially worktree/clone pairs.
- Audit rows are append-only. Stage helpers write them when changing manifest
  rows; readers use them for history and diagnostics.

The dispatcher enforces a global agent-slot cap (`max_active_agents`, default 10). When
the cap is full, no persistent queue is needed; the next heartbeat re-evaluates task
manifest state.

Definition storage is per-domain. Bundled and project YAML land in
`rule_definitions`, `agent_definitions` (optional one-to-one
`agent_step_workflows` child), `session_variable_defaults`, and
`pipeline_definitions`. Dispatch resolves `assigned_agent` through
`agent_definitions`. Runtime step enforcement reads the immutable snapshot
on `agent_step_instances`, not the live child row.

Retired orchestration templates are removed from bundled workflow, agent, and skill roots.
Workflow and agent sync reads top-level YAML, while skill sync reads one directory per skill;
all three soft-delete Gobby-owned installed rows for definitions missing from disk. Therefore
`orchestrator.yaml`, `front-half-orchestrator.yaml`, `dev-orchestrator.yaml`,
`delivery-orchestrator.yaml`, the conductor pipeline, retired `conductor`, `developer`, and
`pipeline-worker` agents, and retired `dev` and `qa` launcher skills must stay out of bundled
install roots. Real PR creation and richer merge/conflict handling are tracked in task
\#13552; this dispatcher only reaches the PR/merge boundary and uses existing merge tools
where they are already available.
