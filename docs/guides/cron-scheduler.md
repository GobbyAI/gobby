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

List jobs:

```bash
uv run gobby cron list
```

Create an interval job:

```bash
uv run gobby cron add "nightly-health" 24h shell --command "uv run gobby status"
```

Run a job immediately:

```bash
uv run gobby cron run nightly-health
```

Inspect run history:

```bash
uv run gobby cron runs nightly-health
```

Use MCP when an agent creates or manages jobs:

```text
list_tools(server_name="gobby-cron")
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

The CLI accepts interval strings such as `300s`, `15m`, `6h`, or `1d`, and cron
expressions for calendar schedules. The storage layer enforces a minimum interval
of 60 seconds.

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

## Run History

Every execution writes a `cron_runs` record with status, timestamps, output,
errors, and metadata. Use run history to answer:

- Did the job start?
- Did the action succeed?
- Was the run skipped because of concurrency limits?
- Which project context did the run use?
- What did the shell command or executor return?

The scheduler also has stale-run recovery so interrupted `running` rows do not
stay active forever.

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
uv run gobby cron add NAME SCHEDULE ACTION_TYPE [options]
uv run gobby cron run NAME_OR_ID
uv run gobby cron toggle NAME_OR_ID --enable
uv run gobby cron runs NAME_OR_ID
uv run gobby cron edit NAME_OR_ID
uv run gobby cron remove NAME_OR_ID
```

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

Follow progressive discovery before calling a tool:

```text
list_mcp_servers
list_tools(server_name="gobby-cron")
get_tool_schema(server_name="gobby-cron", tool_name="list_cron_jobs")
call_tool(server_name="gobby-cron", tool_name="list_cron_jobs", ...)
```

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

_Last verified: 2026-06-01_
