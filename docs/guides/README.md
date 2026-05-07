# Gobby Guides

Use these guides for operating, extending, and contributing to Gobby. Each entry
links to the guide that owns that topic.

## Setup

| Guide | Description |
|-------|-------------|
| [system-requirements.md](system-requirements.md) | Supported operating systems, runtime prerequisites, and backing services |
| [configuration.md](configuration.md) | Daemon, project, and environment configuration reference |

## Core Workflows

| Guide | Description |
|-------|-------------|
| [tasks.md](tasks.md) | Task lifecycle, dependencies, validation, and git-linked closure |
| [task-expansion.md](task-expansion.md) | Run-based task expansion from plans into staged task trees |
| [sessions.md](sessions.md) | Session lifecycle, handoffs, compaction, transcripts, and recovery |
| [memory.md](memory.md) | Persistent memory, vector search, knowledge graph, and session recall |
| [workflows-overview.md](workflows-overview.md) | How workflows, rules, agents, and pipelines fit together |
| [dispatch.md](dispatch.md) | Stage-manifest dispatch, readiness projection, and rule actions |
| [rules.md](rules.md) | Rule engine reference for events, conditions, effects, and enforcement |
| [workflow-rules.md](workflow-rules.md) | Rule-authoring guidance for semantic turn events and safe conditions |
| [pipelines.md](pipelines.md) | Pipeline workflows, data flow, approval gates, and automation steps |
| [tdd-enforcement.md](tdd-enforcement.md) | TDD enforcement with test-first ordering, nudges, and block rules |
| [testing.md](testing.md) | Focused backend, frontend, coverage, and browser test workflows |

## Development Automation

| Guide | Description |
|-------|-------------|
| [agents.md](agents.md) | Agent definitions, spawning, command routing, and run lifecycle |
| [worktrees.md](worktrees.md) | Worktree and clone isolation, merge operations, and task links |
| [orchestration.md](orchestration.md) | Stage dispatch, isolation, review gates, and automated task execution |
| [skills.md](skills.md) | Skill discovery, installation, project scope, and hub integrations |

## Search & Code Navigation

| Guide | Description |
|-------|-------------|
| [search.md](search.md) | Unified search with text, vector, and hybrid modes |
| [code-index.md](code-index.md) | `gcode` indexing, symbol search, and graph navigation |

## Interfaces & Reference

| Guide | Description |
|-------|-------------|
| [cli-commands.md](cli-commands.md) | CLI command reference |
| [mcp-tools.md](mcp-tools.md) | MCP proxy behavior and native Gobby tool reference |
| [http-endpoints.md](http-endpoints.md) | HTTP API route reference |
| [hook-schemas.md](hook-schemas.md) | Hook event schema and adapter payload reference |
| [variables.md](variables.md) | Session variables, conditions, and safe expression evaluation |

## Integrations

| Guide | Description |
|-------|-------------|
| [integrations.md](integrations.md) | GitHub and Linear integration setup |
| [github-issue-triage.md](github-issue-triage.md) | Webhook-first GitHub issue intake, deduplication, and task creation |
| [comm-integrations.md](comm-integrations.md) | Slack, Telegram, Discord, Teams, email, SMS, and Gobby chat adapters |
| [webhooks-and-plugins.md](webhooks-and-plugins.md) | Webhook and plugin development |
| [webhook-action-schema.md](webhook-action-schema.md) | Webhook action schema reference |
| [../cli-integrations/droid.md](../cli-integrations/droid.md) | Factory Droid CLI hooks, MCP, and agent spawning |

## Runtime & Safety

| Guide | Description |
|-------|-------------|
| [sandboxing.md](sandboxing.md) | Operator-facing sandbox configuration and execution boundaries |
| [sandbox-compatibility.md](sandbox-compatibility.md) | Daemon-owned sandbox compatibility matrix and `ghook --diagnose` contract |

## Product Surfaces

| Guide | Description |
|-------|-------------|
| [frontend-style-guide.md](frontend-style-guide.md) | Design tokens, component patterns, and web UI styling rules |
| [voice.md](voice.md) | Local speech-to-text and text-to-speech for web chat voice conversations |

## Writing Specifications

| Guide | Description |
|-------|-------------|
| [spec-writing.md](spec-writing.md) | Writing task specifications and validation criteria |

---

## Learning Paths

### Getting Started

1. Read [system-requirements.md](system-requirements.md) to check prerequisites.
2. Read [configuration.md](configuration.md) to understand daemon and project settings.
3. Read [tasks.md](tasks.md) to learn task lifecycle basics.
4. Read [cli-commands.md](cli-commands.md) for day-to-day commands.

### Automated Development

1. Read [task-expansion.md](task-expansion.md) to understand expanded task trees.
2. Read [orchestration.md](orchestration.md) to follow staged automation.
3. Read [agents.md](agents.md) and [worktrees.md](worktrees.md) for isolated parallel work.
4. Read [workflow-rules.md](workflow-rules.md) before authoring lifecycle rules.

### Building Integrations

1. Read [mcp-tools.md](mcp-tools.md) for the native MCP proxy model.
2. Read [integrations.md](integrations.md) for GitHub and Linear setup.
3. Read [comm-integrations.md](comm-integrations.md) for channel adapters.
4. Read [webhooks-and-plugins.md](webhooks-and-plugins.md) for extension points.

---

## Quick Links

- **Create a task**: `gobby tasks create "Title"` or `create_task` MCP tool
- **List ready work**: `gobby tasks ready` or `list_ready_tasks` MCP tool
- **Spawn an agent**: `gobby agents spawn "Prompt"` or `spawn_agent` MCP tool
- **Create memory**: `gobby memory create "Content"` or `create_memory` MCP tool
- **Session handoff**: `gobby sessions create-handoff` or `set_handoff_context` MCP tool

_Last verified: 2026-05-07_
