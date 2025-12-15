# Gobby Source Tree

> Generated: 2025-12-15 | Scan Level: Exhaustive

## Directory Structure

```
gobby/                                  # Project root
├── pyproject.toml                      # Project configuration, dependencies, build settings
├── README.md                           # Project overview with architecture diagram
├── CLAUDE.md                           # Claude Code development instructions
├── CONTRIBUTING.md                     # Contribution guidelines
├── SECURITY.md                         # Security policy
├── LICENSE                             # MIT License
│
├── src/                                # Source code (62 Python files, ~15k LOC)
│   ├── __init__.py                     # Package init with version export
│   ├── cli.py                          # Click CLI commands [855 lines]
│   ├── runner.py                       # Daemon process entry point [89 lines]
│   │
│   ├── adapters/                       # CLI-specific hook adapters
│   │   ├── base.py                     # BaseAdapter ABC
│   │   ├── claude_code.py              # Claude Code adapter
│   │   ├── codex.py                    # Codex adapter + client [1295 lines]
│   │   └── gemini.py                   # Gemini CLI adapter
│   │
│   ├── config/                         # Configuration management
│   │   ├── app.py                      # DaemonConfig Pydantic model [683 lines]
│   │   └── mcp.py                      # MCP server config models
│   │
│   ├── hooks/                          # Hook event system
│   │   ├── events.py                   # HookEvent, HookResponse models
│   │   ├── hook_manager.py             # Central HookManager coordinator
│   │   └── hook_types.py               # Hook type enums
│   │
│   ├── install/                        # CLI hook dispatcher files
│   │   ├── claude/hooks/               # Claude Code dispatchers
│   │   ├── codex/hooks/                # Codex notify dispatcher
│   │   └── gemini/hooks/               # Gemini CLI dispatcher
│   │
│   ├── llm/                            # Multi-provider LLM abstraction
│   │   ├── base.py                     # LLMProvider ABC
│   │   ├── claude.py                   # Claude provider
│   │   ├── codex.py                    # Codex/OpenAI provider
│   │   ├── gemini.py                   # Gemini provider
│   │   ├── litellm.py                  # LiteLLM provider
│   │   └── service.py                  # LLMService manager
│   │
│   ├── mcp_proxy/                      # MCP client proxy
│   │   ├── actions.py                  # Server add/remove actions
│   │   ├── importer.py                 # Import from GitHub/query
│   │   ├── manager.py                  # MCPClientManager [483 lines]
│   │   ├── server.py                   # FastMCP tools [1624 lines]
│   │   └── stdio.py                    # Stdio transport
│   │
│   ├── servers/                        # Server implementations
│   │   ├── http.py                     # FastAPI HTTP server
│   │   └── websocket.py                # WebSocket server
│   │
│   ├── sessions/                       # Session management
│   │   ├── manager.py                  # SessionManager [369 lines]
│   │   ├── summary.py                  # SummaryGenerator
│   │   └── transcripts/                # Transcript parsers
│   │
│   ├── storage/                        # SQLite storage
│   │   ├── database.py                 # LocalDatabase
│   │   ├── mcp.py                      # MCPDatabaseManager
│   │   ├── migrations.py               # Schema migrations [225 lines]
│   │   ├── projects.py                 # Project CRUD
│   │   └── sessions.py                 # Session CRUD
│   │
│   ├── tools/                          # Tool utilities
│   │   ├── filesystem.py               # Schema cache
│   │   └── summarizer.py               # Description summarizer
│   │
│   └── utils/                          # Utilities
│       ├── context_injector.py         # Session context injection
│       ├── daemon_client.py            # HTTP client
│       ├── git.py                      # Git utilities
│       ├── logging.py                  # Log configuration
│       ├── machine_id.py               # Machine ID
│       ├── project_init.py             # Project initialization
│       └── status.py                   # Status formatting
│
├── tests/                              # Test suite (11 files, ~13k LOC)
│   ├── conftest.py                     # Pytest fixtures
│   ├── test_config.py                  # Configuration tests
│   ├── test_hooks_*.py                 # Hook system tests
│   ├── test_http_server.py             # HTTP server tests
│   ├── test_sessions_manager.py        # Session tests
│   └── test_storage_*.py               # Storage layer tests
│
├── docs/                               # Documentation
│   ├── CLI_COMMANDS.md                 # CLI reference
│   ├── HTTP_ENDPOINTS.md               # HTTP API docs
│   ├── MCP_TOOLS.md                    # MCP tool docs
│   └── brownfield/                     # Generated docs
│
└── .github/                            # GitHub configuration
    └── workflows/                      # CI/CD pipelines
```

## Code Statistics

| Metric | Value |
|--------|-------|
| **Total Python Files** | 73 (62 src + 11 tests) |
| **Estimated LOC** | ~28,000 lines |
| **Test Coverage Target** | 80% |

## Largest Files

| File | Lines | Purpose |
|------|-------|---------|
| `mcp_proxy/server.py` | 1624 | All MCP tool implementations |
| `adapters/codex.py` | 1295 | Codex adapter + AppServerClient |
| `cli.py` | 855 | CLI commands (install, start, stop) |
| `config/app.py` | 683 | Configuration Pydantic models |
| `mcp_proxy/manager.py` | 483 | MCP connection pooling |

## Module Dependencies

```
cli.py
├── config/app.py
├── runner.py
│   ├── servers/http.py
│   │   ├── adapters/*
│   │   ├── hooks/hook_manager.py
│   │   └── mcp_proxy/server.py
│   ├── servers/websocket.py
│   └── mcp_proxy/manager.py
│       └── storage/mcp.py
├── storage/database.py
│   └── storage/migrations.py
└── utils/*
```
