# Python Coding Standards

This document defines the Python coding standards for the Gobby project. These standards are derived from established patterns in the codebase and aligned with the configured tooling (ruff, mypy strict, pytest).

## Table of Contents

- [Tooling Requirements](#tooling-requirements)
- [Type Hints](#type-hints)
- [Import Organization](#import-organization)
- [Naming Conventions](#naming-conventions)
- [Documentation](#documentation)
- [Error Handling](#error-handling)
- [Async Patterns](#async-patterns)
- [Data Models](#data-models)
- [Thread Safety](#thread-safety)
- [Testing](#testing)
- [Module Organization](#module-organization)

---

## Tooling Requirements

All code must pass the following checks before merge:

```bash
# Linting and formatting
uv run ruff check src/
uv run ruff format src/

# Type checking (strict mode)
uv run mypy src/

# Tests with 80% coverage minimum
uv run pytest
```

### Configuration Reference

- **Line length**: 100 characters
- **Target Python version**: 3.13+
- **Mypy mode**: strict (`disallow_untyped_defs = true`)

---

## Type Hints

### Required on All Public Interfaces

All public functions, methods, and class attributes must have complete type annotations.

```python
# Correct
def register_session(
    self,
    external_id: str,
    machine_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Register a new session."""
    ...

# Incorrect - missing return type and parameter types
def register_session(self, external_id, machine_id=None):
    ...
```

### Modern Type Syntax

Use PEP 604 union syntax and modern generic syntax:

```python
# Correct - PEP 604 union syntax
def get_value(key: str) -> str | None:
    ...

# Correct - modern generic syntax
def process_items(items: list[str]) -> dict[str, int]:
    ...

# Incorrect - legacy typing module syntax
from typing import Optional, List, Dict
def get_value(key: str) -> Optional[str]:
    ...
```

### TYPE_CHECKING for Circular Imports

Use `TYPE_CHECKING` blocks to avoid circular import issues:

```python
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.storage.sessions import LocalSessionManager

class SessionManager:
    def __init__(self, config: DaemonConfig) -> None:
        ...
```

### Literal Types for Constrained Values

Use `Literal` for fields with a fixed set of valid values:

```python
from typing import Literal

status: Literal["open", "in_progress", "closed"]
level: Literal["debug", "info", "warning", "error"]
```

---

## Import Organization

Imports must be organized in the following order, separated by blank lines:

1. Future imports
2. Standard library imports
3. Third-party imports
4. Local application imports

```python
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import httpx
from fastapi import FastAPI
from pydantic import BaseModel, Field

from gobby.config.app import DaemonConfig
from gobby.storage.database import HubDatabase
```

### Import Rules

- Use absolute imports with the `gobby.` prefix for local modules
- Use `from module import name` for specific items
- Avoid wildcard imports (`from module import *`)
- Ruff handles import sorting automatically (`I` rule)

---

## Naming Conventions

### Classes

- **PascalCase** for all class names
- Suffix `-Manager` for resource management classes
- Suffix `-Provider` for strategy/adapter implementations
- Suffix `-Error` or `-Exception` for custom exceptions

```python
class LocalSessionManager:      # Manager pattern
class ClaudeLLMProvider:        # Provider pattern
class TaskIDCollisionError:     # Custom exception
```

### Functions and Methods

- **snake_case** for all functions and methods
- Private methods prefixed with single underscore
- Verb-first naming for actions

```python
def register_session(self) -> str:          # Public method
def _get_connection(self) -> Connection:    # Private method
def find_task_by_prefix(prefix: str) -> Task | None:
```

### Variables and Attributes

- **snake_case** for variables and instance attributes
- **UPPER_SNAKE_CASE** for module-level constants
- Private attributes prefixed with single underscore

```python
DEFAULT_DAEMON_PORT = 60887                    # Module constant
DAEMON_STATUS_TEXT = "running"               # Module constant

self._session_mapping: dict[str, str] = {}   # Private attribute
self._cache_lock = threading.Lock()          # Private attribute
```

### Module Files

- **snake_case** for all module file names
- Descriptive names reflecting primary responsibility

```
daemon_client.py
hook_manager.py
task_dependencies.py
```

---

## Documentation

### Module Docstrings

Every module should have a docstring explaining its purpose and key responsibilities:

```python
"""
Session Manager for multi-CLI session management (local-first).

Handles:
- Session registration with hub database storage
- Parent session lookup for context handoff
- Session status updates (active, expired, handoff_ready)

This module is CLI-agnostic and can be used by any CLI integration.
"""
```

### Class Docstrings

Classes should document their purpose and key behaviors:

```python
class SessionManager:
    """
    Manages session lifecycle for AI coding assistants (local-first).

    Provides:
    - Session registration and lookup
    - Parent session discovery for context handoff
    - Status management (active, expired, handoff_ready)

    Thread-safe: Uses locks for session metadata and mapping caches.
    """
```

### Method Docstrings

Public methods should document parameters, return values, and exceptions:

```python
def register_session(
    self,
    external_id: str,
    machine_id: str | None = None,
) -> str:
    """
    Register new session with local storage.

    Args:
        external_id: External session identifier (e.g., Claude Code session ID)
        machine_id: Machine identifier for multi-machine setups

    Returns:
        session_id (database UUID)

    Raises:
        ValueError: If external_id is empty or invalid
    """
```

### When to Skip Docstrings

- Private methods with obvious purpose
- Simple property getters/setters
- Test methods (the test name should be self-documenting)

---

## Error Handling

### Custom Exception Hierarchy

Define specific exceptions for domain errors:

```python
class TaskIDCollisionError(Exception):
    """Raised when a unique task ID cannot be generated."""
    pass

class DependencyCycleError(Exception):
    """Raised when a dependency cycle is detected."""
    pass

class MCPError(Exception):
    """Base exception for MCP client errors."""
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code
```

### Exception Handling Patterns

Catch specific exceptions, not bare `except`:

```python
# Correct - specific exception handling
try:
    result = subprocess.run(command, timeout=timeout, check=False)
except subprocess.TimeoutExpired:
    logger.warning(f"Command timed out after {timeout}s")
    return None
except OSError as e:
    logger.error(f"Failed to execute command: {e}")
    raise

# Incorrect - bare except
try:
    result = subprocess.run(command)
except:
    return None
```

### Graceful Degradation

Design for failure - provide fallback paths where appropriate:

```python
def get_git_remote() -> str | None:
    """Get git remote URL, returning None if unavailable."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None
```

### Logging Errors

Log errors with context and stack traces when appropriate:

```python
try:
    await self.connect()
except ConnectionError as e:
    logger.error(f"Failed to connect to {self.server_name}: {e}", exc_info=True)
    raise
```

---

## Async Patterns

### Async Method Definition

Use `async def` consistently for async operations:

```python
async def connect_all(self) -> None:
    """Connect to all configured MCP servers."""
    tasks = [self._connect_server(name) for name in self.servers]
    await asyncio.gather(*tasks, return_exceptions=True)
```

### Task Management

Use `asyncio.create_task()` for concurrent operations:

```python
async def run(self) -> None:
    websocket_task = asyncio.create_task(self.websocket_server.start())

    try:
        while not self._shutdown_requested:
            await asyncio.sleep(0.5)
    finally:
        websocket_task.cancel()
        try:
            await websocket_task
        except asyncio.CancelledError:
            pass
```

### Timeouts

Always use timeouts for external operations:

```python
try:
    await asyncio.wait_for(self.mcp_proxy.connect_all(), timeout=10.0)
except TimeoutError:
    logger.warning("MCP connection timed out, continuing with partial connectivity")
```

### Context Managers for Async Resources

Use `@asynccontextmanager` for async resource management:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def managed_connection():
    conn = await create_connection()
    try:
        yield conn
    finally:
        await conn.close()
```

---

## Data Models

### Dataclasses for Data Transfer Objects

Use dataclasses for immutable data structures:

```python
from dataclasses import dataclass, field
from typing import Literal

@dataclass
class Task:
    id: str
    project_id: str
    title: str
    status: Literal["open", "in_progress", "closed"]
    priority: int
    description: str | None = None
    labels: list[str] = field(default_factory=list)
```

### Factory Methods for Deserialization

Use `@classmethod` for creating instances from external data:

```python
@dataclass
class Task:
    ...

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Task:
        """Convert database row to Task object."""
        labels_json = row["labels"]
        labels = json.loads(labels_json) if labels_json else []
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            status=row["status"],
            priority=row["priority"],
            description=row["description"],
            labels=labels,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert Task to dictionary for serialization."""
        return {
            "id": self.id,
            "project_id": self.project_id,
            "title": self.title,
            "status": self.status,
            "priority": self.priority,
            "description": self.description,
            "labels": self.labels,
        }
```

### Pydantic for Configuration and Validation

Use Pydantic models for configuration with validation:

```python
from pydantic import BaseModel, Field, field_validator

class WebSocketSettings(BaseModel):
    """WebSocket server configuration."""

    enabled: bool = Field(
        default=True,
        description="Enable WebSocket server",
    )
    port: int = Field(
        default=60888,
        description="Port for WebSocket server",
    )

    @field_validator("port")
    @classmethod
    def validate_port(cls, v: int) -> int:
        """Validate port number is in valid range."""
        if not (1024 <= v <= 65535):
            raise ValueError("Port must be between 1024 and 65535")
        return v
```

---

## Thread Safety

### Lock Usage

Use threading locks for shared mutable state:

```python
import threading

class SessionManager:
    def __init__(self) -> None:
        self._session_mapping: dict[str, str] = {}
        self._mapping_lock = threading.Lock()

    def register(self, external_id: str, session_id: str) -> None:
        with self._mapping_lock:
            self._session_mapping[external_id] = session_id
```

### Database Execution

Use the shared hub database protocol for transaction boundaries:

```python
from gobby.storage.hub.protocol import HubDatabase

class SessionStore:
    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def register(self, session_id: str) -> None:
        with self.db.transaction() as txn:
            txn.execute("INSERT INTO sessions (id) VALUES ($1)", (session_id,))
```

### Context Variables for Async Context

Use `contextvars` for request-scoped data in async code:

```python
import contextvars

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

def set_request_id(request_id: str) -> None:
    request_id_var.set(request_id)

def get_request_id() -> str | None:
    return request_id_var.get()
```

---

## Testing

### Test File Organization

- Test files mirror source structure: `src/storage/tasks.py` -> `tests/test_storage_tasks.py`
- Use descriptive test names that explain the scenario

```python
def test_register_session_returns_uuid():
    ...

def test_register_session_with_duplicate_external_id_updates_existing():
    ...

async def test_connect_all_continues_on_individual_server_failure():
    ...
```

### Fixtures

Use pytest fixtures for test setup:

```python
import pytest

@pytest.fixture
def database(postgres_db):
    """Use the migrated PostgreSQL test hub."""
    yield postgres_db

@pytest.fixture
def task_manager(database):
    """Create a task manager with test database."""
    return LocalTaskManager(database)
```

### Async Tests

Use `pytest-asyncio` for async test functions:

```python
import pytest

@pytest.mark.asyncio
async def test_connect_server_handles_timeout():
    manager = MCPClientManager()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(manager.connect("slow-server"), timeout=0.1)
```

### Test Markers

Use markers to categorize tests:

```python
@pytest.mark.slow
def test_large_file_processing():
    ...

@pytest.mark.integration
def test_database_connection_pool():
    ...

@pytest.mark.e2e
def test_full_session_lifecycle():
    ...
```

---

## Module Organization

### Layer Architecture

Organize code into distinct layers with clear responsibilities:

```
src/
├── cli/              # CLI entry points (Click commands)
├── config/           # Configuration management (Pydantic models)
├── hooks/            # Hook system (events, handlers)
├── llm/              # LLM provider integrations
├── mcp_proxy/        # MCP client management
├── servers/          # HTTP and WebSocket servers
├── sessions/         # Session lifecycle management
├── storage/          # Database and persistence
└── utils/            # Cross-cutting utilities
```

### Single Responsibility

Each module should have a single, clear purpose:

```python
# Good - focused module
# storage/tasks.py - Task CRUD operations only

# Bad - mixed responsibilities
# storage/tasks.py - Task CRUD + email notifications + caching
```

### Dependency Direction

Dependencies should flow inward:

```
CLI -> Services -> Storage
         |
         v
     Configuration
```

- CLI layer depends on services, never the reverse
- Storage layer has no dependencies on higher layers
- Configuration is injected, not imported directly in business logic

### Package Exports

Use `__init__.py` to define public API:

```python
# storage/__init__.py
from gobby.storage.database import HubDatabase
from gobby.storage.sessions import LocalSessionManager
from gobby.storage.tasks import LocalTaskManager

__all__ = ["HubDatabase", "LocalSessionManager", "LocalTaskManager"]
```

---

## Quick Reference

| Category | Standard |
|----------|----------|
| Line length | 100 characters |
| Python version | 3.13+ |
| Type hints | Required on all public interfaces |
| Union syntax | `str \| None` (PEP 604) |
| Imports | Absolute with `gobby.` prefix |
| Class names | PascalCase |
| Function names | snake_case |
| Constants | UPPER_SNAKE_CASE |
| Private members | Single underscore prefix |
| Docstrings | Google style (Args, Returns, Raises) |
| Tests | pytest with 80% coverage minimum |
| Async | Always use timeouts for external calls |

---

## Enforcement

These standards are enforced through:

1. **Pre-commit hooks**: Run ruff and mypy before commit
2. **CI pipeline**: All checks must pass for PR merge
3. **Code review**: Reviewers verify adherence to standards

To set up pre-commit hooks:

```bash
uv run pre-commit install
```
