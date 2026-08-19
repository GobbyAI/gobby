"""Shared fixtures for task MCP tool coverage tests."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-00000000000b"


@pytest.fixture(autouse=True)
def _local_machine_identity(request: pytest.FixtureRequest) -> Iterator[None]:
    """Patch the local machine id and enroll it whenever a database is in scope.

    ``SessionManager.register`` refuses an unenrolled machine, so every test here
    that registers a real session needs the row to exist. Tests that use no
    database keep the patch alone rather than paying for a hub fixture.
    """
    db_fixture = next((name for name in ("db", "temp_db") if name in request.fixturenames), None)
    if db_fixture is not None:
        from gobby.storage.machines import LocalMachineManager
        from tests.fixtures.postgres import TEST_USER_ID

        LocalMachineManager(request.getfixturevalue(db_fixture)).upsert_seen(
            LOCAL_MACHINE_ID, TEST_USER_ID
        )
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def mock_task_manager() -> MagicMock:
    """Create a mock task manager."""
    manager = MagicMock(spec=LocalTaskManager)
    manager.db = MagicMock()
    return manager


@pytest.fixture
def canonical_task_session(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> Session:
    """Register a real session whose project is authoritative for task tools."""
    return SessionManager(temp_db).register(
        external_id="task-tool-test-session",
        machine_id="21000000-0000-4000-8000-00000000000b",
        source="codex",
        project_id=sample_project["id"],
        title="Task tool test session",
    )


@pytest.fixture
def personal_task_session(temp_db: HubDatabase) -> Session:
    """Register a real task-tool session in the personal project."""
    return SessionManager(temp_db).register(
        external_id="personal-task-tool-test-session",
        machine_id="21000000-0000-4000-8000-00000000000b",
        source="codex",
        project_id=None,
        title="Personal task tool test session",
    )


@pytest.fixture
def mock_task_validator() -> AsyncMock:
    """Create a mock task validator."""
    validator = AsyncMock()
    validator.validate_task = AsyncMock()
    return validator


@pytest.fixture
def mock_config() -> MagicMock:
    """Create a mock daemon config."""
    config = MagicMock()
    tasks_config = MagicMock()
    tasks_config.show_result_on_create = False
    validation_config = MagicMock()
    validation_config.auto_generate_on_create = False
    validation_config.auto_generate_on_expand = False
    tasks_config.validation = validation_config
    config.get_gobby_tasks_config.return_value = tasks_config
    return config


@pytest.fixture
def task_registry(
    mock_task_manager: MagicMock,
) -> InternalToolRegistry:
    """Create a task registry with mocked dependencies."""
    return create_task_registry(mock_task_manager)


@pytest.fixture
def sample_task() -> Task:
    """Create a sample task for testing."""
    return Task(
        id="550e8400-e29b-41d4-a716-446655440000",
        project_id="11111111-1111-4111-8111-111111110001",
        title="Test Task",
        priority=2,
        task_type="task",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        updated_at=datetime(2024, 1, 1, tzinfo=UTC),
        description="Test description",
        labels=["test"],
    )


# =============================================================================
# Helper Function Tests
# =============================================================================
