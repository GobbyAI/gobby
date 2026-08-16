# Gobby Project Documentation

> **Updated:** 2026-07-31 | **Version:** 0.5.0

## Project Overview

**Gobby** is a local-first daemon that unifies AI coding assistants (Claude Code, Codex, AGY, Qwen, Droid, and Grok) through a hook interface for session tracking and provides an MCP proxy with progressive tool discovery for efficient access to downstream servers.

### Quick Facts

| Property | Value |
|----------|-------|
| **Type** | Backend + CLI (Daemon) + Web UI |
| **Language** | Python 3.13+ |
| **Framework** | FastAPI + FastMCP + Click |
| **Database** | PostgreSQL local runtime hub |
| **Package Manager** | uv |
| **License** | Apache-2.0 |

## Documentation Index

### Generated Documentation

| Document | Description |
|----------|-------------|
| [Architecture](./architecture.md) | System architecture, components, data flows |
| [Source Tree](./source-tree.md) | Annotated directory structure |
| [Development Guide](./development-guide.md) | Setup, commands, workflows |
| [Technology Stack](./technology-stack.md) | Technologies, patterns, dependencies |
| [Coding Standards](./coding-standards.md) | Python coding conventions and best practices |

### Existing Documentation

| Document | Description |
|----------|-------------|
| [README](../../README.md) | Project overview and quick start |
| [CLAUDE.md](../../CLAUDE.md) | Claude Code development instructions |
| [CLI Commands](../guides/cli-commands.md) | CLI command reference |
| [HTTP Endpoints](../guides/http-endpoints.md) | REST API documentation |
| [MCP Tools](../guides/mcp-tools.md) | MCP tool documentation |
| [Hook Schemas](../guides/hook-schemas.md) | Provider hook payload schemas |
| [Repository History Scrub](./repository-history-scrub.md) | Decision and coordinated rewrite runbook for machine-local state history |
| [Hub-owned files home](./hub-owned-files-home.md) | One hub-host tree for wiki vaults, `_personal`, and `USER.md` |

## Quick Start

```bash
# Install
git clone https://github.com/GobbyAI/gobby.git
cd gobby
uv sync

# Start daemon
uv run gobby start

# Install hooks to your project
cd /path/to/your/project
gobby install

# Check status
gobby status
```

## Architecture Summary

```
┌──────────────────────────────────────────────────────────────┐
│                    CLI Entry (Click)                          │
│              gobby start | stop | status | install           │
└─────────────────────────┬────────────────────────────────────┘
                          │
┌─────────────────────────▼────────────────────────────────────┐
│                    Daemon Layer                               │
│  HTTP Server (:60887) | WebSocket (:60888) | MCP Server        │
└─────────────────────────┬────────────────────────────────────┘
                          │
┌─────────────────────────▼────────────────────────────────────┐
│                   Service Layer                               │
│  HookManager | SessionManager | LLMService | MCPClientManager│
└─────────────────────────┬────────────────────────────────────┘
                          │
┌─────────────────────────▼────────────────────────────────────┐
│                    Data Layer                                 │
│              PostgreSQL hub (bootstrap.yaml database_url)         │
└──────────────────────────────────────────────────────────────┘
```

## Key Capabilities

### Multi-CLI Support
- **Claude Code** - Full hook integration
- **AGY CLI** - Hook integration
- **Codex CLI** - Full hook integration (+ app-server JSON-RPC)
- **Droid** - Hook integration
- **Qwen Code** - Native terminal-hook integration plus ACP web chat
- **Grok CLI** - Hook integration (ACP)

### Session Management
- Automatic session registration
- Cross-session context handoff
- LLM-powered session summaries
- Title synthesis from prompts

### MCP Proxy
- Progressive tool discovery (list → schema → execute)
- Connection pooling for downstream servers
- Support for HTTP, stdio, WebSocket transports
- Dynamic server add/remove

### LLM Integration
- Multi-provider support (Claude, Codex, AGY, Qwen, Droid, local endpoints)
- Subscription-based and BYOK authentication
- Feature-routed LLM calls with profile fallback
- Tool recommendations

## Development Commands

| Command | Description |
|---------|-------------|
| `uv run gobby start` | Start daemon |
| `uv run gobby stop` | Stop daemon |
| `uv run pytest` | Run tests |
| `uv run ruff check src/` | Lint code |
| `uv run mypy src/` | Type check |

## File Locations

| Path | Purpose |
|------|---------|
| `~/.gobby/config.yaml` | Exported config snapshot (runtime config is DB-backed via `config_store`) |
| `~/.gobby/bootstrap.yaml` `database_url` | Runtime PostgreSQL hub DSN |
| `~/.gobby/logs/` | Log files |
| `.gobby/session_summaries/` (project-relative) | Generated summaries |

## Contributing

See [CONTRIBUTING.md](../../CONTRIBUTING.md) for contribution guidelines.

## Security

See [SECURITY.md](../../SECURITY.md) for security policy.

---

*This documentation was generated by the BMAD Document Project workflow.*

_Last verified: 2026-07-31_
