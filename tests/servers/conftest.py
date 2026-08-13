"""Shared fixtures and helpers for server tests."""

from collections.abc import Iterator
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.app_context import ServiceContainer
from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.config.runtime import ConfigRuntime, RuntimeActiveBundle
from gobby.config.runtime_models import ConfigSnapshot
from gobby.hooks.factory import HookManagerFactory
from gobby.servers.auth_service import AuthService
from gobby.servers.http import HTTPServer
from gobby.storage.auth import AuthStore, hash_token
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager

# Sentinel to distinguish "not provided" from "explicitly None"
_NOT_PROVIDED = object()
TEST_LOCAL_TOKEN = "server-test-local-token"


class StubConfigRuntime(ConfigRuntime):
    """Concrete ConfigRuntime double accepted by HTTP-server runtime guards."""

    def __init__(self, snapshot: ConfigSnapshot, *, ready: bool = True) -> None:
        self.current = snapshot
        self._ready = ready

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def snapshot(self) -> ConfigSnapshot:
        return self.current

    def capture(self) -> RuntimeActiveBundle:
        return RuntimeActiveBundle(snapshot=self.current, services=MappingProxyType({}))

    async def reconcile_local_commit(self, revision: int) -> ConfigSnapshot:
        assert revision == self.current.revision
        return self.current


def authenticate_test_server(server: HTTPServer) -> HTTPServer:
    """Mark requests authenticated for tests outside the authentication contract."""
    cast(Any, server.auth_service).is_request_authenticated = MagicMock(return_value=True)
    return server


@pytest.fixture
def authenticated_http_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Authenticate every HTTP request in a module that opts into this fixture."""
    monkeypatch.setattr(
        AuthService,
        "is_request_authenticated",
        lambda _service, _request: True,
    )


@pytest.fixture
def isolated_http_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep generic HTTP lifespan tests independent from operator bootstrap state."""
    original_resolve = HookManagerFactory._resolve_config

    def resolve_config(config: Any | None, runtime: ConfigRuntime | None) -> Any:
        if config is None and runtime is None:
            return DaemonConfig()
        return original_resolve(config, runtime)

    monkeypatch.setattr(HookManagerFactory, "_resolve_config", staticmethod(resolve_config))


def create_http_server(
    port: int = 60887,
    test_mode: bool = True,
    mcp_manager: Any | None = None,
    config: Any = _NOT_PROVIDED,
    session_manager: Any = _NOT_PROVIDED,
    task_manager: Any = _NOT_PROVIDED,
    message_processor: Any | None = None,
    memory_manager: Any | None = None,
    llm_service: Any | None = None,
    memory_backup_manager: Any | None = None,
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
    authenticated_requests: bool = True,
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
        database=db,
        session_manager=sess_mgr,
        task_manager=task_mgr,
        span_storage=span_storage,
        memory_backup_manager=memory_backup_manager,
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

    startup_config = DaemonConfig() if config is _NOT_PROVIDED else config
    server = HTTPServer(
        services=services,
        startup_config=startup_config,
        port=port,
        test_mode=test_mode,
        codex_client=codex_client,
        bootstrap_config=BootstrapConfig(),
    )
    if authenticated_requests:
        authenticate_test_server(server)
    return server


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
    tmp_path: Path,
) -> HTTPServer:
    """Create an HTTP server instance for testing."""
    services = ServiceContainer(
        database=session_storage.db,
        session_manager=session_storage,
        task_manager=MagicMock(),
    )
    server = HTTPServer(
        services=services,
        port=60887,
        test_mode=True,
        bootstrap_config=BootstrapConfig(),
    )
    token_file = tmp_path / "local_cli_token"
    token_file.write_text(TEST_LOCAL_TOKEN)
    AuthStore(session_storage.db).set_local_api_token_hash(hash_token(TEST_LOCAL_TOKEN))
    server.auth_service = AuthService(lambda: session_storage.db, token_file=token_file)
    return server


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
        mock_instance.shutdown_async = AsyncMock()
        with TestClient(
            http_server.app,
            headers={"X-Gobby-Local-Token": TEST_LOCAL_TOKEN},
        ) as client:
            yield client
