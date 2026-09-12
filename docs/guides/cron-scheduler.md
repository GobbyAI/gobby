# Cron Scheduler

The cron scheduler runs recurring, one-shot, and manually-triggered automation
inside the local daemon. It owns scheduled shell commands, agent spawns, pipeline
runs, handler actions, and run history.

## Mental Model

Cron jobs are stored rows with a schedule, action type, action config, enabled
flag, project binding, and next-run timestamp. The scheduler polls for due jobs,
claims execution slots, writes `cron_runs`, and hands the action to the executor.

Execution is isolated from the user's current agent session. Scheduler runs clear
the active session context and set project context from the job's `project_id`.
Pipeline cron runs create system-parented sessions so their activity remains
auditable without pretending to be part of a user's active conversation.

## Quick Start

Cron execution is daemon-backed. Start or verify the daemon before commands that
run jobs immediately or inspect live run state:

```bash
uv run gobby status
uv run gobby start --verbose
```

List jobs:

```bash
uv run gobby cron list
```

Create an interval job:

```bash
uv run gobby cron add -n nightly-health -s 24h -t shell -c '{"command": "uv", "args": ["run", "gobby", "status"]}'
```

Run a job immediately (CLI accepts a UUID or name; get the UUID from `cron list`):

```bash
uv run gobby cron run <job-id>
```

Inspect run history:

```bash
uv run gobby cron runs <job-id>
```

Use MCP when an agent creates or manages jobs:

```text
get_tool_schema(server_name="gobby-cron", tool_name="create_cron_job")
call_tool(server_name="gobby-cron", tool_name="create_cron_job", ...)
```

## Scheduling

The storage model supports these schedule types:

| Type | Use |
|------|-----|
| `interval` | Run every N seconds, minutes, hours, or days |
| `cron` | Run from a cron expression |
| `once` | Run one time at a scheduled timestamp |

The CLI accepts interval strings such as `300s`, `15m`, or `6h` (there is no
day suffix; use `24h`), and cron expressions for calendar schedules. The storage
layer enforces a minimum interval of 60 seconds by clamping smaller values to 60. Supply a
future ISO timestamp for `once` through MCP/HTTP; CLI add accepts interval/cron
schedules, not one-shot timestamps. Omitted timezone resolves to the daemon host
zone and timestamps are stored in UTC. Set an explicit IANA timezone for calendar
intent; prefer five-field cron expressions even though the parser accepts more.

## Actions

The executor supports these action types:

| Action | Use |
|--------|-----|
| `shell` | Run a local command with optional args, cwd, and timeout |
| `agent_spawn` | Start an agent with a prompt and provider settings |
| `pipeline` | Launch a registered pipeline with inputs |
| `handler` | Run an internal registered handler |

MCP creation currently exposes `agent_spawn`, `pipeline`, and `shell` as the
general agent-facing action types.

Pipeline actions may reference tasks by short ref, such as `#123`, in
`inputs.task_id`; the cron MCP layer resolves the ref to the durable task UUID
before storing the job.

### Action Contracts

Cron shell `command` is one executable, with a separate `args` array. There is
no command-line splitting or implicit shell. Set `cwd` explicitly when needed;
without it, the process inherits the daemon working directory. Shell timeout
is 60 seconds by default. Bounded actions also have an outer timeout from
`action_config.timeout_seconds`, otherwise cron configuration; align budgets.

Pipeline config requires `pipeline_name`, with optional `inputs`. Disabled target
pipelines produce `skipped` before execution/session creation. Agent config
requires `prompt`; current provider default is `claude`, agent timeout 300 seconds.
`agent_definition` contributes its prompt/provider selection; this action does
not forward an arbitrary spawn-tool schema. Use a pipeline MCP spawn step when
explicit agent, model, isolation or other spawn controls are required.

For pipeline/agent actions, `overlap_policy` defaults to `skip_if_active`; `allow`
permits overlapping child work but does not bypass admission capacity. Handler
and legacy dispatcher actions are internal implementation paths, not general
agent creation choices. Use `gobby build` for task dispatch.

## Run History

Every execution writes a `cron_runs` record with status, timestamps, output,
errors, and metadata. Use run history to answer:

- Did the job start?
- Did the action succeed?
- Was the run skipped because of concurrency limits?
- Which project context did the run use?
- What did the shell command or executor return?

`run_cron_job` and CLI `cron run` return admission, not the final outcome.
A run can be `pending`, `running`, `completed`, `failed`, `skipped`, or `dispatched`.
`dispatched` means durable child work was linked; inspect `pipeline_execution_id`
or `agent_run_id` and the child's result to establish completion. `list_cron_runs`
returns bounded recent history; it does not provide pipeline-style offset paging.
Use the applicable child's event-driven wait once its ID is known. Do not repeat
manual starts while diagnosing an active run.

Manual run bypasses the schedule and enabled flag, but still enforces capacity,
active-job admission and retired-job rejection. It can test a disabled user job.
A rejection reports `cron_max_concurrent_jobs`, `cron_job_already_running`,
`cron_job_retired`, or scheduler unavailability; repair the cause before retrying.
Scheduled admission claims each due occurrence atomically. Capacity and stale
reconciliation are machine-scoped; scheduler-owner checks protect in-flight work.
Only failures increment backoff; skipped/dispatched outcomes reset it.
Startup recovery reconciles interrupted owned runs and linked child status.
Do not repair cron bookkeeping with direct SQL.

Cron history should represent real scheduled jobs. Internal dispatcher and
pipeline-heartbeat automation is reported through daemon service status, not
through synthetic `cron_runs`.

## System Automation And Nightly Jobs

Dispatcher and pipeline-heartbeat automation now lives in `SystemAutomationLoop`,
controlled by `system_loops.automation.enabled` and
`system_loops.automation.interval_seconds`. The loop runs daemon-owned
maintenance, direct project dispatch ticks, and pipeline heartbeat checks without
creating cron history rows.

Older databases can contain legacy rows such as `gobby:dispatcher` or
`gobby:pipeline-heartbeat`; treat them as migration leftovers rather than the
current automation model.

Cron jobs remain the right fit for deterministic recurring work such as:

- Project maintenance commands.
- Integration sync jobs.
- Nightly pipelines.
- Operator-defined recurring checks.

Prefer cron jobs for deterministic recurring work. Prefer workflows or pipelines
for user-directed multi-step work that needs approval gates or rich state.

## CLI

The cron CLI lives under `gobby cron`:

```bash
uv run gobby cron list
uv run gobby cron add -n NAME -s SCHEDULE -t ACTION_TYPE -c ACTION_CONFIG_JSON [options]
uv run gobby cron run JOB_ID
uv run gobby cron toggle JOB_ID
uv run gobby cron park SYSTEM_JOB_ID
uv run gobby cron wake SYSTEM_JOB_ID
uv run gobby cron runs JOB_ID
uv run gobby cron edit JOB_ID [--enabled | --disabled] [options]
uv run gobby cron remove JOB_ID
```

`toggle` flips the enabled state; use `cron edit --enabled/--disabled` to set
it explicitly. Daemon-managed system jobs keep their enabled state: `park`
clears their next scheduled run, and `wake` recomputes it. `toggle` rejects a
system job and points operators to those commands. Commands accept either a job
UUID or its name; MCP/HTTP lifecycle calls use the UUID. System rows reject
ordinary definition edits, toggles and deletion. `display_name` is an allowed
presentation override (empty string resets it); operator park/wake controls
scheduling without changing enabled ownership. Park is not cancellation of an
active run. Restart-protected jobs require the daemon lifecycle's coordinated
`--wait` or explicit `--force` policy; see [daemon lifecycle commands](./cli-commands.md#daemon-and-setup).

Use the CLI for operator inspection and manual maintenance. Agents should use the
`gobby-cron` MCP server when mutating cron state.

## HTTP

The Web UI Cron Jobs page uses daemon routes for listing, creating, toggling,
running, and inspecting jobs. Route owners live in `src/gobby/servers/routes/`.
When debugging from the browser, inspect fetches under `/api/cron/*`.

## MCP

`gobby-cron` exposes lifecycle tools for:

- Listing jobs.
- Creating a job.
- Getting one job.
- Updating a job.
- Toggling enabled state.
- Deleting a job.
- Running a job now.
- Listing run history.

For a known cron tool without a current-context lease, fetch its schema directly:

```text
get_tool_schema(server_name="gobby-cron", tool_name="list_cron_jobs")
call_tool(server_name="gobby-cron", tool_name="list_cron_jobs", ...)
```

## Safe Authoring And Verification

MCP creation defaults to enabled and has no `enabled` parameter. Start with a
harmless action and a future schedule; creating a near-due mutating job before
verification can execute it immediately. `project_id` defaults to caller project,
then personal project if absent; specify scope deliberately. List filters are
optional, so pass the intended project when inspecting jobs.

For user jobs, set `enabled=false` before changing a live action, perform one
explicit authorized manual test, inspect its child/final result, then re-enable.
Update replaces `action_config`; preserve required keys. MCP update has no
`run_at` parameter; use the operator HTTP surface for one-shot rescheduling.
Create-time pipeline `inputs.task_id` short references resolve to UUIDs; update
has no equivalent conversion, so persist a resolved UUID when replacing inputs.
Deleting a user job also deletes its history; preserve required evidence first.
All mutating guide tests use an isolated test hub and temporary daemon/state.
Never test a new schedule against the user's live jobs.

## File Locations

- `src/gobby/cli/cron.py`: operator CLI.
- `src/gobby/mcp_proxy/tools/cron.py`: agent-facing MCP tools.
- `src/gobby/scheduler/scheduler.py`: polling, concurrency, stale-run recovery.
- `src/gobby/scheduler/executor.py`: action execution.
- `src/gobby/storage/cron.py`: cron persistence.
- `src/gobby/storage/cron_models.py`: cron model types.
- `src/gobby/system_automation.py`: daemon-owned dispatcher and heartbeat loop.
- `web/src/components/cron/`: Cron Jobs page.

## See Also

- [dispatch.md](dispatch.md)
- [pipelines.md](pipelines.md)
- [orchestration.md](orchestration.md)
- [agents.md](agents.md)
- [observability.md](observability.md)

_Last verified: 2026-09-12_
