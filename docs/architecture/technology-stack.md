# Gobby Technology Stack

> Generated: 2025-12-15

## Core Technologies

### Language & Runtime

| Technology | Version | Purpose |
|------------|---------|---------|
| **Python** | 3.11+ | Primary language |
| **asyncio** | stdlib | Async I/O |
| **typing** | stdlib | Type hints |

### Web Framework

| Technology | Version | Purpose |
|------------|---------|---------|
| **FastAPI** | >=0.115.0 | HTTP REST API server |
| **Uvicorn** | >=0.30.0 | ASGI server |
| **websockets** | >=12.0 | WebSocket support |

### MCP Framework

| Technology | Version | Purpose |
|------------|---------|---------|
| **FastMCP** | >=0.2.0 | MCP server implementation |
| **httpx** | >=0.27.0 | Async HTTP client for MCP |

### CLI Framework

| Technology | Version | Purpose |
|------------|---------|---------|
| **Click** | >=8.1.0 | CLI commands |

### Data Validation

| Technology | Version | Purpose |
|------------|---------|---------|
| **Pydantic** | >=2.9.0 | Runtime validation, settings |

### Database

| Technology | Version | Purpose |
|------------|---------|---------|
| **SQLite** | stdlib | Local-first storage |

### Configuration

| Technology | Version | Purpose |
|------------|---------|---------|
| **PyYAML** | >=6.0.3 | YAML config parsing |

### LLM Integration

| Technology | Version | Purpose |
|------------|---------|---------|
| **claude-agent-sdk** | >=0.1.5 | Claude subscription execution |
| **LiteLLM** | >=1.0.0 | Multi-provider abstraction |

### System Utilities

| Technology | Version | Purpose |
|------------|---------|---------|
| **psutil** | >=6.1.0 | Process management |
| **py-machineid** | >=0.6.0 | Machine identification |

## Development Tools

### Testing

| Tool | Version | Purpose |
|------|---------|---------|
| **pytest** | >=8.4.2 | Test framework |
| **pytest-asyncio** | >=1.2.0 | Async test support |
| **pytest-cov** | >=7.0.0 | Coverage reporting |
| **pytest-httpx** | >=0.30.0 | HTTP mocking |
| **pytest-mock** | >=3.14.0 | General mocking |

### Code Quality

| Tool | Version | Purpose |
|------|---------|---------|
| **ruff** | >=0.8.0 | Linting + formatting |
| **mypy** | >=1.8.0 | Static type checking |
| **pre-commit** | >=4.0.0 | Git hooks |

### Build

| Tool | Version | Purpose |
|------|---------|---------|
| **setuptools** | >=61.0 | Package building |
| **uv** | latest | Dependency management |

## Architecture Patterns

### Design Patterns Used

| Pattern | Location | Purpose |
|---------|----------|---------|
| **Adapter** | `adapters/` | CLI-specific hook translation |
| **Factory** | `llm/factory.py` | LLM provider creation |
| **Repository** | `storage/*.py` | Data access abstraction |
| **Service** | `sessions/manager.py` | Business logic encapsulation |
| **Coordinator** | `hooks/hook_manager.py` | Central event handling |

### Concurrency Model

- **Async/await** for I/O-bound operations
- **Thread-local** SQLite connections
- **Threading locks** for shared state
- **Connection pooling** for MCP clients

### Data Flow

```
Inbound: CLI Hook → HTTP → Adapter → HookManager → Service → Storage
Outbound: MCP Tool → MCPClientManager → Downstream Server → Response
```

## Dependency Graph

```
gobby
├── click (CLI)
├── fastapi (HTTP)
│   └── uvicorn (ASGI)
├── fastmcp (MCP)
├── httpx (HTTP client)
├── pydantic (validation)
├── pyyaml (config)
├── websockets (WS)
├── psutil (process)
├── py-machineid (ID)
├── claude-agent-sdk (Claude)
└── litellm (LLM)
```

## Version Constraints

| Constraint | Reason |
|------------|--------|
| Python >=3.11 | Type hints, async improvements |
| Pydantic >=2.9.0 | V2 API required |
| FastAPI >=0.115.0 | Pydantic v2 compatibility |
| FastMCP >=0.2.0 | Latest MCP protocol support |

## CI/CD Stack

| Component | Technology |
|-----------|------------|
| **CI** | GitHub Actions |
| **Testing** | pytest in CI |
| **Coverage** | Codecov |
| **Release** | PyPI trusted publishing |
| **Code Review** | CodeRabbit AI |
