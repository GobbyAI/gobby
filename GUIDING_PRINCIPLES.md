# Guiding Principles

These principles are operational rules for agents working in this repository.

1. Use progressive MCP discovery: list servers, list tools, fetch the specific schema,
   then call the tool.
2. Keep files under 1,000 lines. If an oversized file is found, use gobby-tasks to find
   or create the refactor task under the right parent.
3. Create or claim a Gobby task before editing files.
4. Commit changed work before closing a task. Close with the commit SHA so validation and
   task linkage happen together.
5. Fix failures you encounter during validation unless they need a larger architectural
   task.
6. Treat rule templates as templates. Installed DB rows are the active source of truth.
7. Prefer deterministic rules, storage state, and explicit lifecycle transitions over
   prompt-only control.

## Dispatch

Task automation is owned by deterministic dispatch. The retired conductor and
orchestration pipeline templates are historical tombstones; they are not the active
task-automation model.

Dispatch rules live in `src/gobby/dispatch/rules.py`. A new rule should be a small,
deterministic predicate over task lifecycle, status, labels, automation fields, and
artifacts. It returns an explicit action, then gets added to the ordered rule registry
near its lifecycle stage. If the action needs reasoning, spawn an agent; do not put model
judgment in the dispatcher.

`gobby build` is the opt-in surface. The CLI command, MCP tool
(`gobby-tasks-ops:build_task`), and HTTP route (`POST /api/build`) share the same service
in `src/gobby/build/service.py`.

Build resolves operator intent into stored task state:

- `allow_automation=true` makes the task visible to dispatch.
- `yolo=true` selects deterministic fallback paths where a normal run would escalate.
- `isolation` is one of `none`, `worktree`, or `clone`.
- Stage skips are `stage-:<name>` labels. Profiles are only build-time sugar over skip
  labels, isolation, and yolo.
- `assigned_agent` and `additional_skills` route leaf implementation work.

Use the adjacent dispatch tables through their helpers:

- `task_dispatch_mutex`: per-task leases for side-effecting dispatch actions. Acquire
  before acting, release after, sweep expired leases on startup.
- `task_artifacts`: sparse paths and external pointers. Write worktree/clone path and ID
  pairs atomically.
- `task_lifecycle_events`: append-only lifecycle audit. Lifecycle transition helpers are
  the normal writers.

The dispatcher has a global active-agent slot cap (`max_active_agents`, default 10).
Overflow waits for the next heartbeat.

Retired templates such as `orchestrator.yaml`, `front-half-orchestrator.yaml`,
`dev-orchestrator.yaml`, `delivery-orchestrator.yaml`, and `conductor.yaml` live under
`workflows/*/deprecated/` as archival tombstones. Active bundled sync reads only top-level
YAML and soft-deletes installed rows for retired definitions. PR creation and advanced
merge/conflict work belong to task #12728.
