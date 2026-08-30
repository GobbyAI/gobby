# Gobby HTTP Endpoints

This guide is the HTTP reference for the Gobby 0.5.0 daemon. The daemon exposes
three HTTP-facing surfaces:

- JSON REST endpoints under `/api/*`
- FastMCP HTTP transport available at `/mcp`
- WebSocket proxy routes mounted at `/ws`

The route source of truth is `src/gobby/servers/app_factory.py` plus the router
modules under `src/gobby/servers/routes/`. This reference is not exhaustive:
some surfaces (for example `/api/wiki/*`, `/api/profiles`, `/api/llm`,
`/api/embeddings`, chat attachments, and stage-registry mutation routes) are
documented in their feature guides rather than here.

Admin route helpers define paths relative to `/api/admin`; session and task
helper modules attach their routes to the parent `/api/sessions` and
`/api/tasks` routers. The communications router is conditional and is mounted
only when communications are enabled in daemon config.

## Base URL

```text
http://localhost:60887
```

The default daemon port is `60887`. Bootstrap config can override it with
`daemon_port`; runtime config exposes it as the top-level `daemon_port` field.

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

Daemon auth is mandatory. Protected HTTP requests accept these
credentials, in precedence order:

1. `Authorization: Bearer <local_cli_token>`
2. `X-Gobby-Local-Token: <local_cli_token>`
3. A valid `gobby_session` browser cookie

The plaintext token lives at `$GOBBY_HOME/local_cli_token` (default
`~/.gobby/local_cli_token`) with mode `0600`. External streamable-HTTP MCP
clients must send the bearer header to the `/mcp` endpoint. Gobby CLI, hook,
stdio-proxy, and daemon-aware Rust clients read the token file automatically.

The complete unauthenticated HTTP surface is:

| Match | Public surface | Reason |
| --- | --- | --- |
| Exact | `/` | Production SPA shell |
| Exact | `/api/health` | Lifecycle/liveness health probe |
| Exact | `/api/admin/startup-progress` | CLI startup progress probe |
| Prefix | `/api/auth/` | Login, logout, and auth status |
| Prefix | `/api/comms/webhooks/` | Channel-signature validation runs in the route |
| Prefix | `/api/github/webhooks/` | GitHub HMAC validation runs in the route |
| Prefix | `/assets/` | Production UI assets |
| Exact | `/favicon.ico` | Production UI asset |
| Exact | `/logo.png` | Production UI asset |

Every other `/api/*`, `/mcp*`, and `/memory*` request requires authentication.
The standalone WebSocket server on port `60888` requires bearer auth during the
handshake. The HTTP `/ws` proxy requires a valid browser cookie and injects the
daemon's current token into the upstream connection.

Unauthenticated protected API requests return `401` with:

```json
{
  "error": "Authentication required. CLI clients need ~/.gobby/local_cli_token (run 'gobby install' or 'gobby auth token --rotate'). Browsers: log in."
}
```

## Mounted Non-API Surfaces

| Route | Method | Purpose |
| --- | --- | --- |
| `/mcp` | MCP HTTP transport | FastMCP protocol endpoint. External clients send the local-token bearer header. |
| `/ws` | WebSocket | Cookie-authenticated proxy to the standalone WebSocket server. |
| `/ws/{path}` | WebSocket | Cookie-authenticated proxy subpaths to the standalone WebSocket server. |
| `/assets/*` | `GET` | Production UI assets, mounted only when production UI mode is enabled and assets exist. |
| `/{path}` | `GET` | Production UI SPA fallback, mounted only when production UI mode is enabled. Does not intercept `/api`, `/ws`, or `/health` paths. |

Use `/api/health` for daemon health checks. The main HTTP app does not
register a top-level `/health` REST route.

## Admin

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/admin/startup-progress` | Startup tracker state for CLI progress display. |
| `GET` | `/api/admin/status` | Full daemon status, subsystem health, task/session counts, MCP health, and process metrics. |
| `GET` | `/api/admin/metrics` | Prometheus text exposition. |
| `GET` | `/api/admin/config` | Daemon version, enabled subsystem flags, and selected endpoint hints. |
| `POST` | `/api/admin/shutdown` | Graceful daemon shutdown. |
| `POST` | `/api/admin/restart` | Restart the daemon through service-managed or direct restart helpers. |
| `POST` | `/api/admin/workflows/reload` | Reload installed workflow definitions. |
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
| `POST` | `/api/auth/login` | Authenticate `{email, password, remember_me}` and create a user-owned UI session cookie. |
| `POST` | `/api/auth/logout` | Clear the UI auth session cookie. |
| `GET` | `/api/auth/status` | Return `{authenticated}` for the current request. |

Email lookup is normalized and case-insensitive. Login failures use one generic
response for unknown email and wrong password, perform Argon2id work in both
cases, and share the same per-client rate limit.

## Sessions

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/sessions` | List sessions with query filters and resumability metadata. |
| `POST` | `/api/sessions/register` | Register CLI/session metadata. |
| `POST` | `/api/sessions/web-chat` | Create a durable web-chat session row. |
| `POST` | `/api/sessions/find_current` | Find a session by `external_id`, `machine_id`, `source`, and project. |
| `POST` | `/api/sessions/update_status` | Update a session status. |
| `POST` | `/api/sessions/update_summary` | Update a session summary path. |
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
| `POST` | `/api/sessions/{session_id}/variables/set` | Set a live session variable. |
| `POST` | `/api/sessions/{session_id}/variables/get` | Get a live session variable. |

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

The REST MCP proxy is under `/api/mcp`. The raw FastMCP protocol endpoint remains
available at `/mcp`.

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/mcp/servers` | List servers visible to the resolved project. Each row includes `id`, `scope`, `template`, `template_values`, and `missing_secrets`. |
| `POST` | `/api/mcp/servers` | Add a server. Accepts a manual payload (`name`, `transport`, `command`, `args`, `url`, `env`, `enabled`) or `template`/`values`/`scope`. |
| `PATCH` | `/api/mcp/servers/{name}` | Patch the exact `(name, resolved project)` row. Templated instances reject template-owned runtime fields with `400 template_owned_fields`. |
| `POST` | `/api/mcp/servers/import` | Import MCP server config from a project, GitHub repo, or search query. Honors `scope`/`project_id`. |
| `DELETE` | `/api/mcp/servers/{name}` | Remove the exact `(name, resolved project)` row. |
| `GET` | `/api/mcp/templates` | List templates visible to the resolved project with parameter contracts. |
| `GET` | `/api/mcp/status` | Return MCP registry/status data. |
| `POST` | `/api/mcp/refresh` | Refresh one resolved instance via `refresh_server`. Body may include `server`, `server_id`, `project_id`, `scope`, and `force`. |
| `GET` | `/api/mcp/tools` | List tools across servers. |
| `POST` | `/api/mcp/tools/search` | Search tools. |
| `POST` | `/api/mcp/tools/recommend` | Recommend tools for a task. |
| `POST` | `/api/mcp/tools/embed` | Generate tool embeddings. |
| `POST` | `/api/mcp/tools/schema` | Get one tool schema. Accepts `server_name` or `server_id`. |
| `POST` | `/api/mcp/tools/call` | Call a tool through the progressive-discovery REST endpoint. Accepts `server_name` or `server_id`. |
| `GET` | `/api/mcp/{server_name}/tools` | List tools for one MCP server. |
| `POST` | `/api/mcp/{server_name}/tools/{tool_name}` | Backward-compatible direct tool call endpoint. |

Scope resolution is the shared `resolve_request_scope` table: `scope: "global"` wins, a session-bound request uses its project, an explicit registered `project_id` is used when no session is bound, `scope: "project"` without a project returns `400 project_scope_unresolved`, and the sessionless web-tab payload (`project_id: ""`, no `scope`) lands in the global scope.

`POST /api/mcp/servers` template body:

```json
{
  "name": "demo-instance",
  "template": "demo",
  "values": {"region": "us"},
  "scope": "project",
  "project_id": "11111111-1111-4111-8111-111111111111"
}
```

Manual web-tab body (unchanged):

```json
{
  "name": "my-server",
  "transport": "http",
  "url": "https://example.test/mcp",
  "command": null,
  "args": null,
  "env": {},
  "enabled": true,
  "project_id": ""
}
```

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

Required body fields: `schema_version` (must be `1`), `hook_type`, and
`source`. Requests without `schema_version: 1` are rejected with HTTP 400.
`hook_type` is the provider's native hook name (e.g. `UserPromptSubmit` for
Codex), not a semantic rule event.

```json
{
  "schema_version": 1,
  "hook_type": "UserPromptSubmit",
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

`PATCH /api/tasks/{task_id}` accepts metadata fields such as `title`,
`description`, `priority`, `task_type`, `labels`, `parent_task_id`, `category`,
`validation_criteria`, `allow_automation`, and `isolation`. `isolation` must be
`none`, `worktree`, or `clone`. Retargeting to `worktree` is rejected when clone
artifacts exist, and retargeting to `clone` is rejected when worktree artifacts
exist.

## Agents And Build Automation

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/agents/definitions` | List agent definitions. |
| `POST` | `/api/agents/definitions` | Create an agent definition. |
| `GET` | `/api/agents/definitions/{name}` | Get an agent definition. |
| `GET` | `/api/agents/definitions/{name}/export` | Export an agent definition. |
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
| `GET` | `/api/build/status` | Read compact build state for a task tree or build input. |
| `GET` | `/api/build/dispatch/explain` | Explain dispatcher eligibility without mutation. |
| `GET` | `/api/build/history` | List recent build run and event history. |

`POST /api/build` accepts `input_ref`, `quick`, `skip_stages`, `stage`,
`target_branch`, `agent`, `reset_expansion_output`, `max_active_agents`,
`max_retries`, the planning-seed fields, and build isolation fields. `isolation`
accepts `none`, `worktree`, or `clone`; `workspace_backend` (`worktree` or
`clone`) and `clone` remain backward-compatible aliases. Contradictory isolation
inputs return `400` instead of silently choosing one value.

The planning-seed fields are `planning_seed_state` (`drafted`, `needs_review`,
or `approved`), `completed_plan_review_rounds` (already-completed adversary
rounds, `>= 0`), and `plan_enhancement_rounds` (target constructive
`plan-enhancer` rounds before the adversary gate, `>= 0`, default `0`). Presence
in the request body marks `plan_enhancement_rounds` as explicit, so an explicit
`0` overrides the build profile default.

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
| `GET` | `/api/skills/{skill_id}` | Get a skill. |
| `PUT` | `/api/skills/{skill_id}` | Update a skill. |
| `DELETE` | `/api/skills/{skill_id}` | Delete a skill. |
| `GET` | `/api/skills/{skill_id}/export` | Export a skill. |
| `POST` | `/api/skills/{skill_id}/move-to-project` | Move a skill to project scope. |
| `POST` | `/api/skills/{skill_id}/move-to-installed` | Move a project skill to installed scope. |
| `POST` | `/api/skills/{skill_id}/restore` | Restore a deleted skill. |
| `GET` | `/api/pipelines/definitions` | List pipeline definitions. |
| `POST` | `/api/pipelines/definitions` | Create a pipeline definition. |
| `POST` | `/api/pipelines/definitions/import` | Import a pipeline definition. |
| `GET` | `/api/pipelines/definitions/templates` | List pipeline templates. |
| `GET` | `/api/pipelines/definitions/{definition_id}` | Get a pipeline definition. |
| `PUT` | `/api/pipelines/definitions/{definition_id}` | Update a pipeline definition. |
| `DELETE` | `/api/pipelines/definitions/{definition_id}` | Delete a pipeline definition. |
| `GET` | `/api/pipelines/definitions/{definition_id}/export` | Export a pipeline definition. |
| `POST` | `/api/pipelines/definitions/{definition_id}/duplicate` | Duplicate a pipeline definition. |
| `POST` | `/api/pipelines/definitions/{definition_id}/restore` | Restore a deleted pipeline definition. |
| `POST` | `/api/pipelines/definitions/{definition_id}/restore-from-template` | Restore a pipeline from its template. |
| `POST` | `/api/pipelines/definitions/{definition_id}/move-to-project` | Move a pipeline definition to project scope. |
| `POST` | `/api/pipelines/definitions/{definition_id}/move-to-global` | Move a pipeline definition to global scope. |
| `PUT` | `/api/pipelines/definitions/{definition_id}/toggle` | Toggle a pipeline definition. |
| `GET` | `/api/variables` | List variable definitions. |
| `POST` | `/api/variables` | Create a variable definition. |
| `PUT` | `/api/variables/{ref}` | Update a variable definition. |
| `DELETE` | `/api/variables/{ref}` | Delete a variable definition. |
| `PUT` | `/api/variables/{ref}/toggle` | Toggle a variable definition. |
| `POST` | `/api/variables/{ref}/restore-from-template` | Restore a variable definition from its template. |
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
| `GET` | `/api/code-index/graph` | `gcode graph overview` shim. |
| `GET` | `/api/code-index/graph/file/{file_path:path}` | `gcode graph file` shim. |
| `GET` | `/api/code-index/graph/symbol/{symbol_id}/neighbors` | `gcode graph neighbors` shim. |
| `GET` | `/api/code-index/graph/blast-radius` | `gcode graph blast-radius` shim. |
| `GET` | `/api/code-index/graph/search` | Daemon PostgreSQL symbol autocomplete. |
| `POST` | `/api/code-index/graph/clear` | `gcode graph clear --project-id` shim. |
| `POST` | `/api/code-index/graph/rebuild` | `gcode graph rebuild --project` shim. |
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
| `GET` | `/api/providers/models` | Read the provider-model capability matrix. |
| `GET` | `/api/chat/{conversation_id}/messages` | Read chat messages. |
| `DELETE` | `/api/chat/{conversation_id}/messages` | Delete chat messages. |
| `GET` | `/api/traces` | List traces. |
| `GET` | `/api/traces/{trace_id}` | Get a trace. |
| `GET` | `/api/voice/status` | Voice subsystem status. |
| `POST` | `/api/voice/transcribe` | Transcribe audio. |

### `GET /api/providers/models`

The top-level response is `{ "providers": [...] }`. A matrix-backed provider
entry combines provider availability metadata with:

- `models`: canonical model rows containing aliases, availability, reasoning,
  typed context/output facts, modalities, tool support, `standard`/`fast`
  routes, activation descriptors, multipliers, and field provenance.
- `refresh.generation`: the atomic durable snapshot generation.
- `refresh.sources`: per-source `pending`, `ok`, `stale`, or `error` health,
  attempt counts, timestamps, and the last error.

Refresh errors preserve the last-good model rows. Hidden model rows are omitted.
Bundled Claude and Droid rows seed an empty store; provider collectors replace
their snapshots atomically at startup and every 24 hours.
Configured local generation endpoints may return transport-specific model rows;
their availability does not create canonical matrix facts.

Requests that execute provider models use `speed_mode: "standard" | "fast"`.
Agent spawn accepts it in the REST and MCP request; WebSocket `chat_message`,
chat-completions, and tool-chat accept it per send. Omission selects `standard`,
and the value is not persisted to launch defaults, resume metadata, or chat
session state. Successful execution metadata contains
`speed: { requested, effective, status, reason }`, where status is `standard`,
`fast_configured`, `fast_applied`, `fast_unavailable`, or `fast_degraded`.
`fast_unavailable` fails before provider dispatch.

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
| `POST` | `/api/comms/subscriptions` | Create a project-scoped or explicit-global event subscription. |
| `GET` | `/api/comms/subscriptions` | List event subscriptions, including disabled subscriptions by default. |
| `GET` | `/api/comms/subscriptions/{id}` | Get one event subscription. |
| `PATCH` | `/api/comms/subscriptions/{id}` | Partially update an event subscription. |
| `DELETE` | `/api/comms/subscriptions/{id}` | Delete an event subscription. |
| `GET` | `/api/comms/webhooks/{channel_name}` | Verify a channel webhook. |
| `POST` | `/api/comms/webhooks/{channel_name}` | Receive a channel webhook. |

Subscription creation requires exactly one scope declaration:
`project_id="<uuid>"` or `global_scope=true`. A global subscription cannot set
`session_id`; a session-scoped subscription must reference a session in its
selected project. The response contract includes subscription ID and name,
channel ID and name, `scope.kind` and `scope.project_id`, event pattern,
optional session ID, priority, enabled state, and locally presented ISO
timestamps.

## Error Handling

Routes use FastAPI status codes for validation and service errors:

- `400` for invalid or missing request data
- `401` for unauthenticated protected API, MCP, and memory requests in required mode
- `404` for missing resources or non-mounted UI fallback exclusions
- `500` for unhandled server errors
- `503` when a required daemon manager or subsystem is unavailable

Hook execution is the exception: adapter-compatible hook failures are usually
acknowledged with a response that lets the caller continue, because CLI hooks
must not crash the calling agent runtime.

_Last verified: 2026-08-14_
