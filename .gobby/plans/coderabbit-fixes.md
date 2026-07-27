# CodeRabbit Remediation: Remaining Fixes

This index replaces the original 786-finding monolith with domain-focused work packets. Completed findings and explicit no-fix decisions are excluded.

## Inventory

- Original findings: **786**
- Completed fix findings excluded: **147**
- No-fix decisions excluded: **61**
- Unresolved original fixes retained: **578**
- Discovered unresolved fix retained: **D1**
- Total actionable items: **579**

## Work Packets

| Packet | Original findings | Additional | Scope |
| --- | ---: | ---: | --- |
| [Rust, CodeWiki, and Gwiki](coderabbit-fixes/01-rust-codewiki-gwiki.md) | 57 | — | Rust fixes across gcode, gcore, and gwiki. |
| [Frontend, Documentation, and Bundled Content](coderabbit-fixes/02-frontend-docs-bundled-content.md) | 61 | D1 | Web UI, documentation, shared skills/workflows, packaging metadata, and manifest work. |
| [Agents and AI Runtime](coderabbit-fixes/03-agents-ai-runtime.md) | 58 | — | Agent lifecycle, detection, terminal delivery, AI endpoint, and tool-chat runtime fixes. |
| [CLI, Communications, Hooks, and Integrations](coderabbit-fixes/04-cli-communications-integrations.md) | 68 | — | CLI/configuration, communications adapters, hooks, and external synchronization fixes. |
| [Memory, Projects, Search, and Storage Lifecycle](coderabbit-fixes/05-memory-projects-storage-lifecycle.md) | 57 | — | Memory and project lifecycle, vector/search behavior, and related durable storage fixes. |
| [Tasks, Plans, and Review Learning](coderabbit-fixes/06-tasks-plans-review-learning.md) | 59 | — | Task validation/closure, plan evidence, review learning, and associated storage fixes. |
| [Workflows, Skills, and Orchestration](coderabbit-fixes/07-workflows-skills-orchestration.md) | 55 | — | Workflow state, skill hubs, orchestration wiring, and workflow contract tests. |
| [MCP Proxy and Hub Services](coderabbit-fixes/08-mcp-proxy-hub-services.md) | 53 | — | MCP proxy execution, result handling, connection lifecycle, and PostgreSQL Hub services. |
| [Servers, Sessions, WebSocket, and Runtime Boundaries](coderabbit-fixes/09-servers-sessions-runtime.md) | 53 | — | HTTP/WebSocket routes, session processing, attention state, and runtime ownership boundaries. |
| [Runner, Dispatch, Storage, and Core Contracts](coderabbit-fixes/10-runner-dispatch-storage-core.md) | 57 | — | Runner lifecycle, dispatch/adapters, migrations, core storage, test tooling, and remaining infrastructure fixes. |

## Excluded Finding IDs

Completed: 2, 4-13, 15-26, 33-34, 36-38, 41-42, 45, 47-51, 53-57, 59, 61, 65-68, 70-73, 75, 77-80, 86-90, 92-100, 102-107, 109, 111-119, 122-130, 132, 134, 138, 140-175, 186, 194, 208, 217, 225-226, 229-230, 247, 256, 258, 261-262

No-fix: 1, 3, 14, 27-32, 35, 39-40, 43-44, 46, 52, 58, 60, 62-64, 69, 74, 76, 81-85, 91, 108, 110, 120-121, 131, 139, 227, 238, 259, 298, 366, 378, 396, 402, 416, 435, 462-464, 467, 493, 500, 513, 700, 710, 737, 769-770, 774-775, 778

Finding numbers remain anchored to the original monolith for auditability. Each retained original finding appears exactly once.
