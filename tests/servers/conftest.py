"""Shared fixtures and helpers for server tests."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.app_context import ServiceContainer
from gobby.servers.http import HTTPServer
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager

# Sentinel to distinguish "not provided" from "explicitly None"
_NOT_PROVIDED = object()


def create_http_server(
    port: int = 60887,
    test_mode: bool = True,
    mcp_manager: Any | None = None,
    config: Any | None = None,
    session_manager: Any = _NOT_PROVIDED,
    task_manager: Any = _NOT_PROVIDED,
    task_sync_manager: Any | None = None,
    message_processor: Any | None = None,
    memory_manager: Any | None = None,
    llm_service: Any | None = None,
    memory_sync_manager: Any | None = None,
    task_validator: Any | None = None,
    metrics_manager: Any | None = None,
    agent_runner: Any | None = None,
    worktree_storage: Any | None = None,
    clone_storage: Any | None = None,
    git_manager: Any | None = None,
    project_id: str | None = None,
    websocket_server: Any | None = None,
    codex_client: Any | None = None,
    database: Any | None = None,
    span_storage: Any | None = None,
    transcript_reader: Any | None = None,
) -> HTTPServer:
    """
    Create an HTTPServer instance with the new ServiceContainer API.

    This helper bridges the old-style kwargs to the new ServiceContainer API,
    making it easier to update tests incrementally.
    """
    # Use provided database or get from session_manager
    db = database
    if db is None and session_manager is not None and hasattr(session_manager, "db"):
        db = session_manager.db
    if db is None:
        db = MagicMock()

    # Use MagicMock only if not provided; if explicitly None, use None
    sess_mgr = MagicMock() if session_manager is _NOT_PROVIDED else session_manager
    task_mgr = MagicMock() if task_manager is _NOT_PROVIDED else task_manager

    services = ServiceContainer(
        config=config,
        database=db,
        session_manager=sess_mgr,
        task_manager=task_mgr,
        span_storage=span_storage,
        task_sync_manager=task_sync_manager,
        memory_sync_manager=memory_sync_manager,
        memory_manager=memory_manager,
        llm_service=llm_service,
        mcp_manager=mcp_manager,
        mcp_db_manager=None,
        metrics_manager=metrics_manager,
        agent_runner=agent_runner,
        message_processor=message_processor,
        task_validator=task_validator,
        worktree_storage=worktree_storage,
        clone_storage=clone_storage,
        git_manager=git_manager,
        project_id=project_id,
        websocket_server=websocket_server,
        transcript_reader=transcript_reader,
    )

    return HTTPServer(
        services=services,
        port=port,
        test_mode=test_mode,
        codex_client=codex_client,
    )


@pytest.fixture
def session_storage(temp_db: HubDatabase) -> SessionManager:
    """Create session storage."""
    return SessionManager(temp_db)


@pytest.fixture
def project_storage(temp_db: HubDatabase) -> LocalProjectManager:
    """Create project storage."""
    return LocalProjectManager(temp_db)


@pytest.fixture
def test_project(project_storage: LocalProjectManager, temp_dir: Path) -> dict[str, Any]:
    """Create a test project with project.json file."""
    project = project_storage.create(name="test-project", repo_path=str(temp_dir))

    gobby_dir = temp_dir / ".gobby"
    gobby_dir.mkdir()
    (gobby_dir / "project.json").write_text(f'{{"id": "{project.id}", "name": "test-project"}}')

    return project.to_dict()


@pytest.fixture
def http_server(
    session_storage: SessionManager,
) -> HTTPServer:
    """Create an HTTP server instance for testing."""
    services = ServiceContainer(
        config=None,
        database=session_storage.db,
        session_manager=session_storage,
        task_manager=MagicMock(),
    )
    return HTTPServer(
        services=services,
        port=60887,
        test_mode=True,
    )


@pytest.fixture
def basic_http_server(http_server: HTTPServer) -> HTTPServer:
    """Compatibility alias for legacy HTTP coverage tests."""
    return http_server


@pytest.fixture
def client(http_server: HTTPServer) -> Iterator[TestClient]:
    """Create a test client for the HTTP server."""
    with patch("gobby.servers.app_factory.HookManager") as MockHM:
        mock_instance = MockHM.return_value
        mock_instance._stop_registry = MagicMock()
        mock_instance.shutdown = MagicMock()
        with TestClient(http_server.app) as client:
            yield client
