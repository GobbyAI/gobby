# Gobby Guides

Documentation guides for using Gobby's features.

## Core Features

| Guide | Description |
|-------|-------------|
| [tasks.md](tasks.md) | Task management with dependencies, validation, and git sync |
| [sessions.md](sessions.md) | Session lifecycle, handoffs, and context management |
| [memory.md](memory.md) | Persistent knowledge across sessions |
| [workflows-overview.md](workflows-overview.md) | How rules, agents, and pipelines fit together |
| [rules.md](rules.md) | Rule engine — events, effects, conditions, enforcement |
| [pipelines.md](pipelines.md) | Pipeline workflows with data flow and approval gates |
| [lobster-migration.md](lobster-migration.md) | Migrating from Lobster to Gobby pipelines |
| [search.md](search.md) | Unified search with TF-IDF, embeddings, and hybrid modes |
| [testing.md](testing.md) | Token-efficient test/lint/typecheck infrastructure for agents |

## Parallel Development

| Guide | Description |
|-------|-------------|
| [agents.md](agents.md) | Subagent spawning and management |
| [worktrees.md](worktrees.md) | Git worktrees, clones, and merge operations |

## Reference

| Guide | Description |
|-------|-------------|
| [mcp-tools.md](mcp-tools.md) | Complete MCP tool reference (145+ tools) |
| [cli-commands.md](cli-commands.md) | Full CLI command reference |
| [configuration.md](configuration.md) | Full configuration reference (config.yaml, project.json) |

## Integrations

| Guide | Description |
|-------|-------------|
| [integrations.md](integrations.md) | GitHub and Linear integration |

## Extensibility

| Guide | Description |
|-------|-------------|
| [skills.md](skills.md) | Skill discovery and management |
| [hook-schemas.md](hook-schemas.md) | Hook event system |
| [webhooks-and-plugins.md](webhooks-and-plugins.md) | Webhook and plugin development |
| [variables.md](variables.md) | Session variables, conditions, and safe expression evaluator |

## API & Architecture

| Guide | Description |
|-------|-------------|
| [http-endpoints.md](http-endpoints.md) | HTTP API reference |
| [webhook-action-schema.md](webhook-action-schema.md) | Webhook action schemas |
| [sandboxing.md](sandboxing.md) | Code execution sandboxing |

## Frontend

| Guide | Description |
|-------|-------------|
| [frontend-style-guide.md](frontend-style-guide.md) | Design tokens, component patterns, and styling rules for the web UI |

## Writing Specifications

| Guide | Description |
|-------|-------------|
| [spec-writing.md](spec-writing.md) | Writing task specifications |

---

## Learning Paths

### Getting Started

1. Read [tasks.md](tasks.md) - Understand task management
2. Read [sessions.md](sessions.md) - Understand session lifecycle
3. Read [cli-commands.md](cli-commands.md) - Learn CLI basics

### Advanced Usage

1. Read [agents.md](agents.md) - Spawn subagents
2. Read [worktrees.md](worktrees.md) - Parallel development
3. Read [rules.md](rules.md) - Enforce processes

### Building Integrations

1. Read [mcp-tools.md](mcp-tools.md) - Understand MCP tools
2. Read [webhooks-and-plugins.md](webhooks-and-plugins.md) - Create plugins
3. Read [hook-schemas.md](hook-schemas.md) - Hook into events

---

## Quick Links

- **Create a task**: `gobby tasks create "Title"` or `create_task` MCP tool
- **List ready work**: `gobby tasks ready` or `list_ready_tasks` MCP tool
- **Spawn an agent**: `gobby agents spawn "Prompt"` or `spawn_agent` MCP tool
- **Create memory**: `gobby memory create "Content"` or `create_memory` MCP tool
- **Session handoff**: `gobby sessions create-handoff` or `create_handoff` MCP tool
