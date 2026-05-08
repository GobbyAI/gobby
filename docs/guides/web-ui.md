# Web UI

The Web UI is the browser surface for operating a local Gobby daemon. It owns the
chat shell, project dashboard, task and workflow views, source-control panels,
configuration pages, and the operational dashboard.

## Mental Model

Gobby runs the React app from `web/` beside the daemon HTTP and WebSocket
services. In the default local development layout:

- Web UI: `http://localhost:60889`
- HTTP API: `http://localhost:60887`
- WebSocket API: `ws://localhost:60888`
- Tailscale UI URL, when enabled: shown by `gobby status`

The app is a hash-routed shell. `web/src/App.tsx` reads the active hash, renders
the matching page, and keeps shared state for the selected project, chat session,
providers, MCP servers, skills, settings, and voice status. The header exposes
the current project and connection state. The left navigation exposes the main
work surfaces: Chat, Project, Tasks, Workflows, Cron Jobs, Reports, Traces,
Memory, Skills, MCP, Integrations, and Configuration. The dashboard is available
at `/#dashboard`.

Chrome DevTools MCP was used to inspect the running local UI while preparing this
guide. The inspection verified top-level navigation, Chat, Dashboard, Project
overview, Project Settings, Source Control, and network calls including
`/api/admin/status`, `/api/providers/models`, `/api/projects`, and
`/api/source-control/*`.

## Quick Start

Start or inspect the daemon:

```bash
uv run gobby start --verbose
uv run gobby status
```

Open the web app:

```text
http://localhost:60889/#chat
```

Check the backend directly when the UI appears disconnected:

```bash
curl -sS http://localhost:60887/api/auth/status
curl -sS http://localhost:60887/api/admin/status
```

Use the Tailscale URL from `gobby status` when operating from another trusted
device.

## Navigation

The application shell lives in `web/src/App.tsx` and
`web/src/components/app/appNavigation.tsx`.

| Surface | Route | Primary owner |
|---------|-------|---------------|
| Chat | `/#chat` | `web/src/components/chat/ChatPage.tsx` |
| Project | `/#projects` | `web/src/components/projects/ProjectsPage.tsx` |
| Tasks | `/#tasks` | `web/src/components/tasks/TasksPage.tsx` |
| Workflows | `/#workflows` | `web/src/components/workflows/WorkflowsPage.tsx` |
| Cron Jobs | `/#cron` | `web/src/components/cron/CronJobsPage.tsx` |
| Reports | `/#reports` | `web/src/components/reports/ReportsPage.tsx` |
| Traces | `/#traces` | `web/src/components/traces/TracesPage.tsx` |
| Memory | `/#memory` | `web/src/components/memory/MemoryPage.tsx` |
| Skills | `/#skills` | `web/src/components/skills/SkillsPage.tsx` |
| MCP | `/#mcp` | `web/src/components/mcp/McpPage.tsx` |
| Integrations | `/#integrations` | `web/src/components/integrations/IntegrationsPage.tsx` |
| Configuration | `/#configuration` | `web/src/components/configuration/ConfigurationPage.tsx` |
| Dashboard | `/#dashboard` | `web/src/components/dashboard/DashboardPage.tsx` |

Project-scoped pages should read the active project from the shell rather than
re-resolving it independently. Most hooks pass `project_id` through query
parameters.

## Web Chat

Web chat combines HTTP session reads with WebSocket streaming:

- Session creation and replay use `/api/sessions`, `/api/sessions/{id}`, and
  `/api/sessions/{id}/messages`.
- Web-chat session creation uses `/api/sessions/web-chat`.
- Provider and model controls read `/api/providers` and `/api/providers/models`.
- Chat settings persist through `/api/config/ui-settings`.
- Voice status uses `/api/voice/status`.
- Canvas and artifact events arrive over the same live UI channel used by chat.

The main React owners are `web/src/hooks/useChat/*`,
`web/src/components/chat/ChatPage.tsx`, and provider controls under
`web/src/components/chat/`.

## Dashboard

The dashboard aggregates runtime health, task counts, sessions, token usage,
savings, memory totals, and metrics charts. It is backed by admin and metrics
routes:

- `/api/admin/status`
- `/api/admin/stats`
- `/api/admin/usage`
- `/api/admin/savings`
- `/api/admin/tokens/timeseries`
- `/api/metrics/snapshots`

The dashboard hooks live in `web/src/hooks/useDashboard.ts`,
`web/src/hooks/useUsage.ts`, `web/src/hooks/useSavings.ts`,
`web/src/hooks/useMetrics.ts`, and `web/src/hooks/useTokenTimeSeries.ts`.

## Projects And Source Control

`ProjectsPage` owns project overview, files, source control, GitHub issues and
pull requests, CI/CD, and settings. The Source Control tab uses
`web/src/hooks/useSourceControl.ts` and `web/src/components/source-control/`.

Source-control API calls are rooted at `/api/source-control`:

- `/api/source-control/status`
- `/api/source-control/branches`
- `/api/source-control/worktrees`
- `/api/source-control/clones`
- `/api/source-control/prs`
- `/api/source-control/issues`
- `/api/source-control/cicd/runs`

The Settings tab edits project integration fields such as GitHub URL, GitHub
repository, Linear team ID, Linear project ID, and project tool approval rules.

## CLI

The UI is operated through daemon commands rather than a separate web CLI:

```bash
uv run gobby start --verbose
uv run gobby status
uv run gobby restart
```

Use `gobby status` as the source of truth for local ports, Tailscale status, and
service health.

## HTTP

The browser normally calls the web origin on `:60889`; the web server proxies
API routes to the daemon services. Direct API debugging can use the HTTP daemon
port from `gobby status`.

Useful checks:

```bash
curl -sS http://localhost:60887/api/admin/status
curl -sS http://localhost:60887/api/providers/models
curl -sS http://localhost:60887/api/projects
```

Authentication state is exposed through `/api/auth/status`. When local auth is
enabled, login and logout use `/api/auth/login` and `/api/auth/logout`.

## MCP

The Web UI does not replace the MCP proxy. It visualizes and operates the same
daemon state that agents reach through MCP tools. For UI research or debugging,
use progressive discovery against `chrome-devtools`:

1. `list_mcp_servers`
2. `list_tools(server_name="chrome-devtools")`
3. `get_tool_schema(...)`
4. `call_tool(...)`

For product behavior, prefer native Gobby MCP servers such as `gobby-tasks`,
`gobby-cron`, `gobby-metrics`, `gobby-canvas`, `gobby-memory`, and
`gobby-skills`.

## File Locations

- `web/src/App.tsx`: application shell and route selection.
- `web/src/components/app/appNavigation.tsx`: top-level navigation metadata.
- `web/src/components/chat/`: chat page, provider controls, activity panel.
- `web/src/components/dashboard/`: dashboard cards and charts.
- `web/src/components/projects/`: project overview, files, settings, source
  control tabs.
- `web/src/components/source-control/`: branch, worktree, clone, issue, PR, and
  CI/CD views.
- `web/src/hooks/`: API hooks used by UI surfaces.
- `src/gobby/servers/routes/`: HTTP route owners.
- `src/gobby/servers/websocket/`: WebSocket chat and live event owners.

## See Also

- [frontend-style-guide.md](frontend-style-guide.md)
- [providers-and-models.md](providers-and-models.md)
- [canvas-artifacts.md](canvas-artifacts.md)
- [observability.md](observability.md)
- [http-endpoints.md](http-endpoints.md)

_Last verified: 2026-05-08_
