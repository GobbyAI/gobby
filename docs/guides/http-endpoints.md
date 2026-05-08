# Gobby HTTP Endpoints

This guide is the HTTP reference for the Gobby 0.4.0 daemon. The daemon exposes
three HTTP-facing surfaces:

- JSON REST endpoints under `/api/*`
- FastMCP HTTP transport mounted at `/mcp`
- WebSocket proxy routes mounted at `/ws`

The route source of truth is `src/gobby/servers/app_factory.py` plus the router
modules under `src/gobby/servers/routes/`.

Admin route helpers define paths relative to `/api/admin`; session and task
helper modules attach their routes to the parent `/api/sessions` and
`/api/tasks` routers. The communications router is conditional and is mounted
only when communications are enabled in daemon config.

## Base URL

```text
http://localhost:60887
```

The default daemon port is `60887`. Bootstrap config can override it with
`daemon_port`; runtime config exposes it as `daemon.daemon_port`.

## Request Context

Most agent and MCP calls should include project/session context headers when
they are made outside the stdio MCP proxy:

| Header | Purpose |
| --- | --- |
| `X-Gobby-Session-Id` | Identifies the calling Gobby session. Accepts canonical session UUIDs and session refs when the server can resolve them. |
| `X-Gobby-Project-Id` | Identifies the project scope when no session header is available. |

The project context middleware reads these headers before route handlers run.
For MCP tool execution, the session header identifies the caller/workflow
context; any `session_id` inside the JSON body remains a target-tool argument.

## Authentication

Authentication is optional. When no UI credentials are configured, every route
passes through unchanged. When username/password auth is configured, API routes
require a valid UI session cookie except the public surfaces below:

- `/api/auth/*`
- `/api/hooks/*`
- `/api/github/webhooks/*`
- `/api/sessions/*`
- `/api/mcp/*`
- `/api/admin/health`
- `/api/admin/status`
- `/api/admin/metrics`
- `/api/admin/config`
- `/api/health*` legacy health-check paths if present
- `/assets/*`
- `/favicon.ico`
- `/logo.png`
- `/ws` and `/ws/*`

Unauthenticated protected API requests return `401` with:

```json
{
  "error": "Authentication required"
}
```

## Mounted Non-API Surfaces

| Route | Method | Purpose |
| --- | --- | --- |
| `/mcp` | MCP HTTP transport | FastMCP protocol mount. JSON-RPC clients use this mount directly. |
| `/__gobby__/canvas/*` | `GET` | Static Canvas sandbox files from `~/.gobby/canvas`. |
| `/ws` | WebSocket | Proxy to the standalone WebSocket server. |
| `/ws/{path}` | WebSocket | Proxy subpaths to the standalone WebSocket server. |
| `/assets/*` | `GET` | Production UI assets, mounted only when production UI mode is enabled and assets exist. |
| `/{path}` | `GET` | Production UI SPA fallback, mounted only when production UI mode is enabled. Does not intercept `/api`, `/ws`, or `/health` paths. |

Use `/api/admin/health` for daemon health checks. The main HTTP app does not
register a top-level `/health` REST route.

## Admin

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/admin/health` | Lightweight startup health check. |
| `GET` | `/api/admin/startup-progress` | Startup tracker state for CLI progress display. |
| `GET` | `/api/admin/status` | Full daemon status, subsystem health, task/session counts, MCP health, and process metrics. |
| `GET` | `/api/admin/metrics` | Prometheus text exposition. |
| `GET` | `/api/admin/config` | Daemon version, enabled subsystem flags, and selected endpoint hints. |
| `POST` | `/api/admin/shutdown` | Graceful daemon shutdown. |
| `POST` | `/api/admin/restart` | Restart the daemon through service-managed or direct restart helpers. |
| `POST` | `/api/admin/workflows/reload` | Reload installed workflow definitions. |
| `GET` | `/api/admin/setup-state` | Read web onboarding setup state. |
| `POST` | `/api/admin/setup-state` | Update web onboarding setup state. |
| `GET` | `/api/admin/savings` | Current token/cost savings tracker data. |
| `GET` | `/api/admin/savings/cumulative` | Cumulative savings totals. |
| `POST` | `/api/admin/savings/record` | Record a savings event. |
| `GET` | `/api/admin/stats` | Aggregate daemon statistics. |
| `GET` | `/api/admin/usage` | Usage metrics. |
| `GET` | `/api/admin/tokens/timeseries` | Token usage time series. |
| `POST` | `/api/admin/test/register-project` | Test helper for registering a project. |
| `POST` | `/api/admin/test/register-agent` | Test helper for registering an agent run. |
| `DELETE` | `/api/admin/test/unregister-agent/{run_id}` | Test helper for removing an agent run. |
| `POST` | `/api/admin/test/set-session-usage` | Test helper for setting session usage. |

### `GET /api/admin/status`

Returns a JSON object with daemon health and runtime details:

```json
{
  "status": "healthy",
  "server": {
    "port": 60887,
    "test_mode": false,
    "running": true,
    "uptime_seconds": 3600
  },
  "process": {
    "memory_rss_mb": 45.2,
    "memory_vms_mb": 120.5,
    "cpu_percent": 2.5,
    "num_threads": 8
  },
  "sessions": {
    "active": 1,
    "paused": 0,
    "handoff_ready": 0,
    "total": 12
  },
  "tasks": {
    "ready": 3,
    "in_progress": 1,
    "closed": 20
  },
  "mcp_servers": {
    "gobby-tasks": {
      "connected": true,
      "transport": "internal",
      "health": "healthy",
      "tool_count": 31
    }
  },
  "response_time_ms": 5.2
}
```

## Auth

| Method | Route | Purpose |
| --- | --- | --- |
| `POST` | `/api/auth/login` | Create a UI auth session cookie. |
| `POST` | `/api/auth/logout` | Clear the UI auth session cookie. |
| `GET` | `/api/auth/status` | Report whether auth is enabled and whether the request is authenticated. |

## Sessions

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/sessions` | List sessions with query filters and resumability metadata. |
| `POST` | `/api/sessions/register` | Register CLI/session metadata. |
| `POST` | `/api/sessions/web-chat` | Create a durable web-chat session row. |
| `POST` | `/api/sessions/find_current` | Find a session by `external_id`, `machine_id`, `source`, and project. |
| `POST` | `/api/sessions/find_parent` | Find the most recent parent session for a handoff. |
| `POST` | `/api/sessions/update_status` | Update a session status. |
| `POST` | `/api/sessions/update_summary` | Update a session summary path. |
| `POST` | `/api/sessions/statusline` | Record statusline activity for a CLI session. |
| `GET` | `/api/sessions/usage` | Return session usage breakdowns. |
| `POST` | `/api/sessions/bulk-move` | Move session rows to another project. |
| `GET` | `/api/sessions/{session_id}` | Get one session. |
| `POST` | `/api/sessions/{session_id}/expire` | Expire a session. |
| `POST` | `/api/sessions/{session_id}/rename` | Rename a session. |
| `POST` | `/api/sessions/{session_id}/generate-summary` | Generate a session summary. |
| `GET` | `/api/sessions/{session_id}/messages` | Read persisted session messages. |
| `GET` | `/api/sessions/{session_id}/transcript/status` | Inspect transcript availability. |
| `GET` | `/api/sessions/{session_id}/transcript` | Read transcript content. |
| `POST` | `/api/sessions/{session_id}/restore-transcript` | Restore an archived transcript. |
| `GET` | `/api/sessions/{session_id}/token-events` | Read token events for a session. |
| `POST` | `/api/sessions/{session_id}/stop` | Set a stop signal. |
| `GET` | `/api/sessions/{session_id}/stop` | Read stop-signal state. |
| `DELETE` | `/api/sessions/{session_id}/stop` | Clear stop-signal state. |

### `POST /api/sessions/register`

Required body field: `external_id`.

```json
{
  "external_id": "session-abc123",
  "machine_id": "machine-xyz",
  "transcript_path": "/path/to/transcript.jsonl",
  "title": "Session Title",
  "source": "Claude Code",
  "parent_session_id": "uuid-of-parent",
  "status": "active",
  "project_id": "project-uuid",
  "project_path": "/path/to/project",
  "git_branch": "main",
  "cwd": "/current/working/dir",
  "sandbox_enabled": true
}
```

Response:

```json
{
  "status": "registered",
  "external_id": "session-abc123",
  "id": "generated-uuid",
  "machine_id": "machine-xyz"
}
```

### `POST /api/sessions/find_current`

Required body fields: `external_id`, `machine_id`, `source`, and either
`project_id` or `cwd`.

```json
{
  "external_id": "session-abc123",
  "machine_id": "machine-xyz",
  "source": "Claude Code",
  "cwd": "/current/working/dir"
}
```

Returns `{ "session": null }` when no matching session exists.

## MCP Proxy

The REST MCP proxy is under `/api/mcp`. The raw FastMCP protocol mount remains
available at `/mcp`.

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/mcp/servers` | List internal and configured external MCP servers. |
| `POST` | `/api/mcp/servers` | Add an MCP server config. |
| `POST` | `/api/mcp/servers/import` | Import MCP server config from a project, GitHub repo, or search query. |
| `DELETE` | `/api/mcp/servers/{name}` | Remove an MCP server config. |
| `GET` | `/api/mcp/status` | Return MCP registry/status data. |
| `POST` | `/api/mcp/refresh` | Refresh MCP tool registry data. |
| `GET` | `/api/mcp/tools` | List tools across servers. |
| `POST` | `/api/mcp/tools/search` | Search tools. |
| `POST` | `/api/mcp/tools/recommend` | Recommend tools for a task. |
| `POST` | `/api/mcp/tools/embed` | Generate tool embeddings. |
| `POST` | `/api/mcp/tools/schema` | Get one tool schema. |
| `POST` | `/api/mcp/tools/call` | Call a tool through the progressive-discovery REST endpoint. |
| `GET` | `/api/mcp/{server_name}/tools` | List tools for one MCP server. |
| `POST` | `/api/mcp/{server_name}/tools/{tool_name}` | Backward-compatible direct tool call endpoint. |

### Preferred Tool Calls

Use progressive discovery for agent/tool clients:

```http
GET /api/mcp/servers
GET /api/mcp/{server_name}/tools
POST /api/mcp/tools/schema
POST /api/mcp/tools/call
```

The discovery sequence is: list servers, list tools for the selected server,
fetch the schema for the selected tool, then call the tool.

`POST /api/mcp/tools/schema` body:

```json
{
  "server_name": "gobby-tasks",
  "tool_name": "get_task"
}
```

`POST /api/mcp/tools/call` body:

```json
{
  "server_name": "gobby-tasks",
  "tool_name": "get_task",
  "arguments": {
    "task_id": "#14375",
    "brief": false
  }
}
```

Send the caller context in `X-Gobby-Session-Id` or `X-Gobby-Project-Id`
headers. Keep any `session_id` field inside `arguments` for the target MCP tool
itself.

The legacy `POST /api/mcp/{server_name}/tools/{tool_name}` route still exists,
but new automation should prefer the schema/call endpoints so discovery and
context tracking are consistent.

## Hooks And Webhooks

| Method | Route | Purpose |
| --- | --- | --- |
| `POST` | `/api/hooks/execute` | Execute a CLI hook envelope through the adapter layer. |
| `GET` | `/api/webhooks` | List configured MCP webhooks. |
| `POST` | `/api/webhooks/test` | Test an MCP webhook. |
| `POST` | `/api/github/webhooks/triage/{project_id}` | Receive GitHub issue triage webhook events. |

### `POST /api/hooks/execute`

Required body fields after envelope normalization: `hook_type` and `source`.

```json
{
  "hook_type": "turn_start",
  "source": "codex",
  "input_data": {
    "prompt": "Implement the task"
  }
}
```

Hook rule authors should use semantic lifecycle events such as `turn_start` and
`turn_end`. Raw provider/runtime events such as `before_agent`, `after_agent`,
and `stop` are adapter details. Agent termination is a separate lifecycle step
and still requires `end_agent_run`.

## Tasks And Stages

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/tasks` | List tasks. |
| `POST` | `/api/tasks` | Create a task. |
| `GET` | `/api/tasks/{task_id}` | Get one task. |
| `PATCH` | `/api/tasks/{task_id}` | Update task fields. |
| `DELETE` | `/api/tasks/{task_id}` | Delete a task. |
| `POST` | `/api/tasks/{task_id}/claim` | Claim a task. |
| `POST` | `/api/tasks/{task_id}/release-claim` | Release a task claim. |
| `POST` | `/api/tasks/{task_id}/escalate` | Escalate a task. |
| `POST` | `/api/tasks/{task_id}/de-escalate` | Return an escalated task to its preserved stage. |
| `POST` | `/api/tasks/{task_id}/close` | Close a task. |
| `POST` | `/api/tasks/{task_id}/reopen` | Reopen a task. |
| `GET` | `/api/tasks/{task_id}/comments` | List task comments. |
| `POST` | `/api/tasks/{task_id}/comments` | Create a task comment. |
| `DELETE` | `/api/tasks/{task_id}/comments/{comment_id}` | Delete a task comment. |
| `GET` | `/api/tasks/{task_id}/dependencies` | Read task dependency tree. |
| `POST` | `/api/tasks/{task_id}/dependencies` | Add a dependency. |
| `DELETE` | `/api/tasks/{task_id}/dependencies/{depends_on_id}` | Remove a dependency. |
| `GET` | `/api/tasks/{task_id}/stages` | Read a task stage manifest. |
| `PATCH` | `/api/tasks/{task_id}/stages/{stage_name}` | Apply a stage transition or stage manifest mutation. |
| `GET` | `/api/stages/registry` | List stage registry entries. |
| `GET` | `/api/task-types/{task_type}/default-stages` | Read default stages for a task type. |

## Agents And Build Automation

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/agents/definitions` | List agent definitions. |
| `POST` | `/api/agents/definitions` | Create an agent definition. |
| `GET` | `/api/agents/definitions/{name}` | Get an agent definition. |
| `GET` | `/api/agents/definitions/{name}/export` | Export an agent definition. |
| `POST` | `/api/agents/definitions/{name}/install` | Install a bundled agent template. |
| `POST` | `/api/agents/definitions/import/{name}` | Import an agent definition. |
| `PUT` | `/api/agents/definitions/{definition_id}` | Update an agent definition. |
| `DELETE` | `/api/agents/definitions/{definition_id}` | Delete an agent definition. |
| `POST` | `/api/agents/definitions/{definition_id}/restore` | Restore a deleted agent definition. |
| `PATCH` | `/api/agents/definitions/{definition_id}/rules` | Patch agent rules. |
| `PATCH` | `/api/agents/definitions/{definition_id}/rule-selectors` | Patch agent rule selectors. |
| `PATCH` | `/api/agents/definitions/{definition_id}/variables` | Patch agent variables. |
| `GET` | `/api/agents/running` | List running agents. |
| `GET` | `/api/agents/runs` | List agent runs. |
| `GET` | `/api/agents/runs/{run_id}` | Get one agent run. |
| `POST` | `/api/agents/runs/{run_id}/cancel` | Cancel an agent run. |
| `POST` | `/api/agents/spawn` | Spawn one agent. |
| `POST` | `/api/agents/spawn/batch` | Spawn multiple agents. |
| `POST` | `/api/agents/spawn/prompt-preview` | Preview a spawn prompt. |
| `GET` | `/api/agents/launch-defaults` | Read agent launch defaults. |
| `PUT` | `/api/agents/launch-defaults` | Save agent launch defaults. |
| `POST` | `/api/build` | Start lifecycle automation. |
| `POST` | `/api/build/stop` | Stop lifecycle automation. |
| `POST` | `/api/build/resume` | Resume lifecycle automation. |
| `POST` | `/api/build/clean` | Clean lifecycle automation artifacts. |
| `POST` | `/api/build/restart` | Restart lifecycle automation. |

## Memory, Skills, Workflows, And Rules

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/memories` | List memories. |
| `POST` | `/api/memories` | Create a memory. |
| `GET` | `/api/memories/search` | Search memories. |
| `GET` | `/api/memories/stats` | Memory statistics. |
| `GET` | `/api/memories/{memory_id}` | Get a memory. |
| `PUT` | `/api/memories/{memory_id}` | Update a memory. |
| `DELETE` | `/api/memories/{memory_id}` | Delete a memory. |
| `GET` | `/api/memories/graph` | Memory graph overview. |
| `GET` | `/api/memories/graph/entities` | Knowledge-graph entities. |
| `GET` | `/api/memories/graph/entities/{entity_key}/neighbors` | Entity neighbors. |
| `POST` | `/api/memories/graph/clear` | Clear the knowledge graph. |
| `POST` | `/api/memories/graph/rebuild` | Rebuild the knowledge graph. |
| `GET` | `/api/memories/graph/rebuild/status` | Read knowledge-graph rebuild status. |
| `POST` | `/api/memories/crossrefs/rebuild` | Rebuild memory cross-references. |
| `POST` | `/api/memories/embeddings/reindex` | Reindex memory embeddings. |
| `POST` | `/api/memories/reconcile` | Reconcile memory stores. |
| `POST` | `/api/memories/invalidate` | Invalidate memory caches. |
| `GET` | `/api/skills` | List skills. |
| `POST` | `/api/skills` | Create a skill. |
| `GET` | `/api/skills/search` | Search skills. |
| `GET` | `/api/skills/stats` | Skill statistics. |
| `GET` | `/api/skills/hubs` | List configured skill hubs. |
| `GET` | `/api/skills/hubs/search` | Search skill hubs. |
| `POST` | `/api/skills/hubs/install` | Install a skill from a hub. |
| `POST` | `/api/skills/import` | Import a skill. |
| `POST` | `/api/skills/scan` | Scan a skill. |
| `POST` | `/api/skills/restore-defaults` | Restore default skills. |
| `POST` | `/api/skills/install-all-templates` | Legacy template install endpoint. |
| `GET` | `/api/skills/{skill_id}` | Get a skill. |
| `PUT` | `/api/skills/{skill_id}` | Update a skill. |
| `DELETE` | `/api/skills/{skill_id}` | Delete a skill. |
| `GET` | `/api/skills/{skill_id}/export` | Export a skill. |
| `POST` | `/api/skills/{skill_id}/install` | Install a bundled skill template. |
| `POST` | `/api/skills/{skill_id}/move-to-project` | Move a skill to project scope. |
| `POST` | `/api/skills/{skill_id}/move-to-installed` | Move a project skill to installed scope. |
| `POST` | `/api/skills/{skill_id}/restore` | Restore a deleted skill. |
| `GET` | `/api/workflows` | List workflows. |
| `POST` | `/api/workflows` | Create a workflow. |
| `POST` | `/api/workflows/import` | Import a workflow. |
| `GET` | `/api/workflows/templates` | List workflow templates. |
| `POST` | `/api/workflows/install-all-templates` | Legacy template install endpoint. |
| `POST` | `/api/workflows/variables/set` | Set a workflow variable. |
| `POST` | `/api/workflows/variables/get` | Get a workflow variable. |
| `GET` | `/api/workflows/{definition_id}` | Get a workflow. |
| `PUT` | `/api/workflows/{definition_id}` | Update a workflow. |
| `DELETE` | `/api/workflows/{definition_id}` | Delete a workflow. |
| `GET` | `/api/workflows/{definition_id}/export` | Export a workflow. |
| `POST` | `/api/workflows/{definition_id}/duplicate` | Duplicate a workflow. |
| `POST` | `/api/workflows/{definition_id}/install` | Install a bundled workflow template. |
| `POST` | `/api/workflows/{definition_id}/restore` | Restore a deleted workflow. |
| `POST` | `/api/workflows/{definition_id}/restore-from-template` | Restore a workflow from its template. |
| `POST` | `/api/workflows/{definition_id}/move-to-project` | Move a workflow to project scope. |
| `POST` | `/api/workflows/{definition_id}/move-to-global` | Move a workflow to global scope. |
| `PUT` | `/api/workflows/{definition_id}/toggle` | Toggle a workflow. |
| `GET` | `/api/rules` | List rules. |
| `POST` | `/api/rules` | Create a rule. |
| `PUT` | `/api/rules` | Replace/update the rules collection. |
| `GET` | `/api/rules/groups` | List rule groups. |
| `GET` | `/api/rules/tags` | List rule tags. |
| `PUT` | `/api/rules/bulk-toggle` | Toggle multiple rules. |
| `GET` | `/api/rules/{name}` | Get a rule. |
| `PUT` | `/api/rules/{name}` | Update a rule. |
| `DELETE` | `/api/rules/{name}` | Delete a rule. |
| `PUT` | `/api/rules/{name}/toggle` | Toggle one rule. |

## Source Control, Files, Projects, And Config

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/source-control/status` | Repository status. |
| `GET` | `/api/source-control/branches` | List branches. |
| `POST` | `/api/source-control/branches/checkout` | Check out a branch. |
| `GET` | `/api/source-control/branches/{branch_name:path}/commits` | List branch commits. |
| `GET` | `/api/source-control/diff` | Get repository diff. |
| `GET` | `/api/source-control/prs` | List pull requests. |
| `GET` | `/api/source-control/prs/{number}` | Get a pull request. |
| `GET` | `/api/source-control/prs/{number}/checks` | Get PR checks. |
| `GET` | `/api/source-control/issues` | List issues. |
| `GET` | `/api/source-control/issues/{number}` | Get an issue. |
| `GET` | `/api/source-control/cicd/runs` | List CI/CD runs. |
| `GET` | `/api/source-control/worktrees` | List worktrees. |
| `GET` | `/api/source-control/worktrees/stats` | Worktree statistics. |
| `POST` | `/api/source-control/worktrees/cleanup` | Clean worktrees. |
| `DELETE` | `/api/source-control/worktrees/{worktree_id}` | Delete a worktree. |
| `POST` | `/api/source-control/worktrees/{worktree_id}/sync` | Sync a worktree. |
| `GET` | `/api/source-control/clones` | List clones. |
| `DELETE` | `/api/source-control/clones/{clone_id}` | Delete a clone. |
| `POST` | `/api/source-control/clones/{clone_id}/sync` | Sync a clone. |
| `GET` | `/api/files/projects` | List project roots for file browsing. |
| `GET` | `/api/files/tree` | List a directory. |
| `GET` | `/api/files/read` | Read a file. |
| `GET` | `/api/files/image` | Read an image. |
| `POST` | `/api/files/write` | Write a file. |
| `GET` | `/api/files/git-status` | File-browser git status. |
| `GET` | `/api/files/git-diff` | File-browser git diff. |
| `GET` | `/api/projects` | List projects. |
| `GET` | `/api/projects/{project_id}` | Get a project. |
| `PUT` | `/api/projects/{project_id}` | Update a project. |
| `DELETE` | `/api/projects/{project_id}` | Delete a project. |
| `GET` | `/api/projects/{project_id}/github-triage` | Read GitHub triage config. |
| `PUT` | `/api/projects/{project_id}/github-triage` | Update GitHub triage config. |
| `GET` | `/api/config/schema` | Read config schema. |
| `GET` | `/api/config/values` | Read config values. |
| `PUT` | `/api/config/values` | Save config values. |
| `POST` | `/api/config/values/validate` | Validate config values. |
| `POST` | `/api/config/values/reset` | Reset config values. |
| `GET` | `/api/config/template` | Read config template. |
| `PUT` | `/api/config/template` | Save config template. |
| `GET` | `/api/config/secrets` | List secret names. |
| `POST` | `/api/config/secrets` | Save a secret. |
| `DELETE` | `/api/config/secrets/{name}` | Delete a secret. |
| `GET` | `/api/config/prompts` | List prompt overrides. |
| `GET` | `/api/config/prompts/{path:path}` | Read a prompt override. |
| `PUT` | `/api/config/prompts/{path:path}` | Save a prompt override. |
| `DELETE` | `/api/config/prompts/{path:path}` | Delete a prompt override. |
| `POST` | `/api/config/export` | Export config. |
| `POST` | `/api/config/import` | Import config. |
| `GET` | `/api/config/ui-settings` | Read UI settings. |
| `PUT` | `/api/config/ui-settings` | Save UI settings. |
| `DELETE` | `/api/config/ui-settings/{key}` | Delete a UI setting. |
| `GET` | `/api/config/tool-approvals/global` | Read global tool approval rules. |
| `PUT` | `/api/config/tool-approvals/global` | Save global tool approval rules. |

## Code Index, Metrics, Pipelines, And Other Feature APIs

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/code-index/graph` | Code graph overview. |
| `GET` | `/api/code-index/graph/file/{file_path:path}` | File graph data. |
| `GET` | `/api/code-index/graph/symbol/{symbol_id}/neighbors` | Symbol neighbors. |
| `GET` | `/api/code-index/graph/blast-radius` | Blast-radius query. |
| `GET` | `/api/code-index/graph/search` | Search code graph. |
| `POST` | `/api/code-index/graph/clear` | Clear graph projection. |
| `POST` | `/api/code-index/graph/rebuild` | Rebuild graph projection. |
| `POST` | `/api/code-index/invalidate` | Invalidate code index data. |
| `GET` | `/api/metrics/current` | Current metrics snapshot. |
| `GET` | `/api/metrics/snapshots` | Historical metric snapshots. |
| `GET` | `/api/pipelines/executions` | List pipeline executions. |
| `GET` | `/api/pipelines/executions/search` | Search pipeline executions. |
| `GET` | `/api/pipelines/{execution_id}` | Get pipeline execution. |
| `POST` | `/api/pipelines/run` | Run a pipeline. |
| `POST` | `/api/pipelines/approve/{token}` | Approve a pipeline gate. |
| `POST` | `/api/pipelines/reject/{token}` | Reject a pipeline gate. |
| `GET` | `/api/cron/jobs` | List cron jobs. |
| `POST` | `/api/cron/jobs` | Create a cron job. |
| `GET` | `/api/cron/jobs/{job_id}` | Get a cron job. |
| `PATCH` | `/api/cron/jobs/{job_id}` | Update a cron job. |
| `DELETE` | `/api/cron/jobs/{job_id}` | Delete a cron job. |
| `POST` | `/api/cron/jobs/{job_id}/toggle` | Toggle a cron job. |
| `POST` | `/api/cron/jobs/{job_id}/run` | Run a cron job now. |
| `GET` | `/api/cron/jobs/{job_id}/runs` | List cron job runs. |
| `GET` | `/api/cron/runs/{run_id}` | Get a cron run. |
| `GET` | `/api/providers` | List CLI providers and availability. |
| `GET` | `/api/providers/models` | List provider model catalogs. |
| `GET` | `/api/chat/{conversation_id}/messages` | Read chat messages. |
| `DELETE` | `/api/chat/{conversation_id}/messages` | Delete chat messages. |
| `GET` | `/api/traces` | List traces. |
| `GET` | `/api/traces/{trace_id}` | Get a trace. |
| `GET` | `/api/voice/status` | Voice subsystem status. |
| `POST` | `/api/voice/transcribe` | Transcribe audio. |

## Communications

The communications router is registered only when communications are enabled in
daemon config.

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/comms/channels` | List communication channels. |
| `POST` | `/api/comms/channels` | Create a channel. |
| `PUT` | `/api/comms/channels/{channel_id}` | Update a channel. |
| `DELETE` | `/api/comms/channels/{channel_id}` | Remove a channel. |
| `GET` | `/api/comms/channels/{channel_id}/status` | Channel status. |
| `GET` | `/api/comms/messages` | List communication messages. |
| `GET` | `/api/comms/webhooks/{channel_name}` | Verify a channel webhook. |
| `POST` | `/api/comms/webhooks/{channel_name}` | Receive a channel webhook. |

## Error Handling

Routes use FastAPI status codes for validation and service errors:

- `400` for invalid or missing request data
- `401` for unauthenticated protected API requests when UI auth is enabled
- `404` for missing resources or non-mounted UI fallback exclusions
- `500` for unhandled server errors
- `503` when a required daemon manager or subsystem is unavailable

Hook execution is the exception: adapter-compatible hook failures are usually
acknowledged with a response that lets the caller continue, because CLI hooks
must not crash the calling agent runtime.

_Last verified: 2026-05-07_
