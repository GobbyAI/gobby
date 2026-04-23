"""Shared fixtures for task MCP tool coverage tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.sync.tasks import TaskSyncManager


@pytest.fixture
def mock_task_manager():
    """Create a mock task manager."""
    manager = MagicMock(spec=LocalTaskManager)
    manager.db = MagicMock()
    return manager


@pytest.fixture
def mock_sync_manager():
    """Create a mock sync manager."""
    return MagicMock(spec=TaskSyncManager)


@pytest.fixture
def mock_task_validator():
    """Create a mock task validator."""
    validator = AsyncMock()
    validator.validate_task = AsyncMock()
    return validator


@pytest.fixture
def mock_config():
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
def task_registry(mock_task_manager, mock_sync_manager):
    """Create a task registry with mocked dependencies."""
    return create_task_registry(mock_task_manager, mock_sync_manager)


@pytest.fixture
def sample_task():
    """Create a sample task for testing."""
    return Task(
        id="550e8400-e29b-41d4-a716-446655440000",
        project_id="proj-1",
        title="Test Task",
        status="open",
        priority=2,
        task_type="task",
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        description="Test description",
        labels=["test"],
    )


# =============================================================================
# Helper Function Tests
# =============================================================================


