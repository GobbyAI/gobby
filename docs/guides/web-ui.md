# Web UI

The Web UI is the browser surface for operating a local Gobby daemon. It owns the
chat shell, project dashboard, task and workflow views, source-control panels,
configuration pages, and the operational dashboard.

## Mental Model

The daemon owns the UI lifecycle whenever persistent `ui.enabled` is `true`.
Production installs serve the built React app from the daemon HTTP port. In a
source checkout, `dev` mode (and `auto` when source is available) starts and
stops the frontend development server with the daemon on a separate port.

- Installed Web UI and HTTP API: `http://localhost:60887`
- Dev Web UI: `http://localhost:60889`
- WebSocket API: `ws://localhost:60888`
- Tailscale UI URL, when enabled: shown by `gobby status`

The app is a hash-routed shell. `web/src/App.tsx` reads the active hash, renders
the matching page, and keeps shared state for the selected project, chat session,
providers, MCP servers, skills, settings, and voice status. The header exposes
the current project and connection state. The left navigation exposes the main
work surfaces: Chat, Project, Workflows, Cron Jobs, Reports, Traces,
Memory, Skills, Integrations, and Configuration. The dashboard is available
at `/#dashboard`. Task and MCP views are reached through the chat Activity
panel rather than the left navigation.

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
http://localhost:60887/#chat
```

Create or reset browser credentials, then restart the daemon:

```bash
uv run gobby auth credentials
uv run gobby restart
```

The login page exchanges those credentials for the HTTP-only `gobby_session`
cookie. The same cookie authorizes API requests and the `/ws` browser proxy.

Check the backend directly when the UI appears disconnected:

```bash
curl -sS http://localhost:60887/api/auth/status
TOKEN="$(tr -d '\r\n' < "${GOBBY_HOME:-$HOME/.gobby}/local_cli_token")"
curl -sS -H "Authorization: Bearer $TOKEN" \
  http://localhost:60887/api/admin/status
```

Use the Tailscale URL from `gobby status` when operating from another trusted
device.

Removing browser credentials uses `gobby auth credentials --remove`. Daemon API
auth remains required and token-based clients continue to work. Setting
bootstrap `auth_mode: disabled` opens daemon surfaces and is intended only for
an explicitly trusted isolated environment.

## Navigation

The application shell lives in `web/src/App.tsx` and
`web/src/components/app/appNavigation.tsx`.

| Surface | Route | Primary owner |
|---------|-------|---------------|
| Chat | `/#chat` | `web/src/components/chat/ChatPage.tsx` |
| Project | `/#projects` | `web/src/components/projects/ProjectsPage.tsx` |
| Workflows | `/#workflows` | `web/src/components/workflows/WorkflowsPage.tsx` |
| Cron Jobs | `/#cron` | `web/src/components/CronJobsPage.tsx` |
| Reports | `/#reports` | `web/src/components/workflows/ReportsPage.tsx` |
| Traces | `/#traces` | `web/src/components/traces/TracesPage.tsx` |
| Memory | `/#memory` | `web/src/components/memory/MemoryPage.tsx` |
| Skills | `/#skills` | `web/src/components/skills/SkillsPage.tsx` |
| Integrations | `/#integrations` | `web/src/components/integrations/IntegrationsPage.tsx` |
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
- Artifact events arrive over the same live UI channel used by chat.

The main React owners are `web/src/hooks/useChat/*`,
`web/src/components/chat/ChatPage.tsx`, and provider controls under
`web/src/components/chat/`.

### Chat Attachments

Stored chat attachments upload through `POST /api/chat/attachments`, are
referenced in WebSocket `chat_message` or `send_to_cli_session` frames as
`attachments: [{ "id": "..." }]`, and are bound when a message is accepted.
Clients should retry only after the user changes the attachment set when the
server returns `INVALID_ATTACHMENT`; this code means the attachment payload,
count, ID, type, or size is invalid. `ATTACHMENT_ERROR` means processing failed
after validation, so clients may offer a normal retry.

Limits are enforced on both HTTP upload and WebSocket binding:

- Web chat stored attachments use the configured per-file, per-message count,
  and per-message total limits from chat configuration.
- Terminal proxy attachments accept at most 10 legacy base64 files per message,
  each up to 25 MB, with a 250 MB total cap.
- MIME sniffing must match the declared type except for generic binary types,
  text-compatible types, and recognized zip container formats.

## Dashboard

The dashboard aggregates runtime health, task counts, sessions, token usage,
memory totals, and metrics charts. It is backed by admin and metrics routes:

- `/api/admin/status`
- `/api/admin/stats`
- `/api/admin/usage`
- `/api/admin/tokens/timeseries`
- `/api/metrics/snapshots`

The dashboard hooks live in `web/src/hooks/useDashboard.ts`,
`web/src/hooks/useUsage.ts`, `web/src/hooks/useMetrics.ts`, and
`web/src/hooks/useTokenTimeSeries.ts`.

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

Both startup commands follow persistent `ui.enabled`; set it to `false` to run
the daemon without the UI. The existing `gobby ui` commands remain available for
explicit UI development, build, and status operations.

Use `gobby status` as the source of truth for local ports, Tailscale status, and
service health.

## HTTP

The installed browser app normally calls the daemon origin on `:60887`. During
frontend development, the `:60889` dev server proxies API routes to the daemon
services. Direct API debugging can use the HTTP daemon port from `gobby status`.

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
use context-aware discovery against `chrome-devtools`: call a leased known tool
directly, or call `get_tool_schema` directly before an unleased known tool. Use
`list_tools` only when the tool name is unknown and `list_mcp_servers` only when
the server or registry is unknown.

For product behavior, prefer native Gobby MCP servers such as `gobby-tasks`,
`gobby-cron`, `gobby-metrics`, `gobby-memory`, and `gobby-skills`. File previews
are UI-owned: FilesTab uses `/api/files/read` and `/api/files/image`, while plan
review and generated-image rendering use their chat transports directly.

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
- [artifacts.md](artifacts.md)
- [observability.md](observability.md)
- [http-endpoints.md](http-endpoints.md)

_Last verified: 2026-07-10_
