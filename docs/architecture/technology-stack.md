# Gobby Technology Stack

> Updated: 2026-07-31

## Core Technologies

### Language & Runtime

| Technology | Version | Purpose |
|------------|---------|---------|
| **Python** | 3.13+ | Primary language |
| **asyncio** | stdlib | Async I/O |
| **typing** | stdlib | Type hints |

### Web Framework

| Technology | Version | Purpose |
|------------|---------|---------|
| **FastAPI** | >=0.136.0,!=0.136.3 | HTTP REST API server |
| **Uvicorn** | >=0.30.0 | ASGI server |
| **websockets** | >=15.0 | WebSocket support |

### MCP Framework

| Technology | Version | Purpose |
|------------|---------|---------|
| **FastMCP** | >=3.2.0 | MCP server implementation (security floor: CVE-2025-64340, CVE-2026-27124) |
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
| **PostgreSQL** | 18+ | Local runtime hub database |
| **pg_search** | ParadeDB extension | BM25 keyword indexes |
| **psycopg** | 3.x | PostgreSQL driver and pools |

### Configuration

| Technology | Version | Purpose |
|------------|---------|---------|
| **PyYAML** | >=6.0.3 | YAML config parsing |

### LLM Integration

| Technology | Version | Purpose |
|------------|---------|---------|
| **claude-agent-sdk** | >=0.1.81 | Claude subscription execution |
| **anthropic** | >=0.75.0 | Claude API client |
| **openai** | >=1.0.0 | OpenAI-compatible API client (incl. local endpoints) |

### System Utilities

| Technology | Version | Purpose |
|------------|---------|---------|
| **psutil** | >=6.1.0 | Process management |
| **py-machineid** | >=0.6.0 | Machine identification |

## Development Tools

### Testing

| Tool | Version | Purpose |
|------|---------|---------|
| **pytest** | >=9.0.3 | Test framework |
| **pytest-asyncio** | >=1.2.0 | Async test support |
| **pytest-cov** | >=7.0.0 | Coverage reporting |
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
| **setuptools** | >=64.0 | Package building (via a custom `build_backend` wrapper) |
| **uv** | latest | Dependency management |

## Architecture Patterns

### Design Patterns Used

| Pattern | Location | Purpose |
|---------|----------|---------|
| **Adapter** | `adapters/` | CLI-specific hook translation |
| **Factory** | `llm/factory.py` | LLM provider creation |
| **Repository** | `storage/*.py` | Data access abstraction |
| **Service** | `sessions/lifecycle.py` | Business logic encapsulation |
| **Coordinator** | `hooks/hook_manager.py` | Central event handling |

### Concurrency Model

- **Async/await** for I/O-bound operations
- **PostgreSQL** transactions and connection pools
- **Threading locks** for shared state
- **Connection pooling** for MCP clients

### Data Flow

```
Inbound: CLI Hook → HTTP → Adapter → HookManager → Service → Storage
Outbound: MCP Tool → MCPClientManager → Downstream Server → Response
```

## Dependency Graph

Key runtime dependencies (see `pyproject.toml` for the full, current list —
it also includes aiohttp, aiofiles, jinja2, msgspec, croniter, falkordb,
qdrant-client, cryptography, and opentelemetry instrumentation):

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
├── psycopg (PostgreSQL)
├── psutil (process)
├── py-machineid (ID)
├── claude-agent-sdk (Claude)
├── anthropic (Claude API)
└── openai (OpenAI-compatible APIs)
```

The web UI under `web/` is a separate TypeScript stack (React + Vite +
Tailwind, tested with Vitest); see `web/package.json`.

## Version Constraints

| Constraint | Reason |
|------------|--------|
| Python >=3.13 | Type hints, async improvements |
| Pydantic >=2.9.0 | V2 API required |
| FastAPI >=0.136.0,!=0.136.3 | Pydantic v2 compatibility; excluded broken release |
| FastMCP >=3.2.0 | Security floor (CVE-2025-64340, CVE-2026-27124) |

## CI/CD Stack

| Component | Technology |
|-----------|------------|
| **CI** | GitHub Actions |
| **Testing** | pytest in CI |
| **Coverage** | Codecov |
| **Release** | PyPI trusted publishing |
| **Code Review** | CodeRabbit AI |

_Last verified: 2026-07-31_
