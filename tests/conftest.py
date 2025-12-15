"""Pytest configuration and shared fixtures for Gobby tests."""

import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from gobby.config.app import DaemonConfig
from gobby.storage.database import LocalDatabase
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.migrations import run_migrations
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import LocalSessionManager


@pytest.fixture
def temp_dir() -> Iterator[Path]:
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def temp_db(temp_dir: Path) -> Iterator[LocalDatabase]:
    """Create a temporary database for testing."""
    db_path = temp_dir / "test.db"
    db = LocalDatabase(db_path)
    run_migrations(db)
    yield db
    db.close()


@pytest.fixture
def session_manager(temp_db: LocalDatabase) -> LocalSessionManager:
    """Create a session manager with temp database."""
    return LocalSessionManager(temp_db)


@pytest.fixture
def project_manager(temp_db: LocalDatabase) -> LocalProjectManager:
    """Create a project manager with temp database."""
    return LocalProjectManager(temp_db)


@pytest.fixture
def mcp_manager(temp_db: LocalDatabase) -> LocalMCPManager:
    """Create an MCP manager with temp database."""
    return LocalMCPManager(temp_db)


@pytest.fixture
def sample_project(project_manager: LocalProjectManager) -> dict:
    """Create a sample project for testing."""
    project = project_manager.create(
        name="test-project",
        repo_path="/tmp/test-project",
        github_url="https://github.com/test/test-project",
    )
    return project.to_dict()


@pytest.fixture
def default_config() -> DaemonConfig:
    """Create a default DaemonConfig for testing."""
    return DaemonConfig()


@pytest.fixture
def mock_machine_id() -> Iterator[str]:
    """Mock the machine ID for consistent testing."""
    machine_id = "test-machine-id-12345"
    with patch("gobby.utils.machine_id.get_machine_id", return_value=machine_id):
        yield machine_id


@pytest.fixture
def mock_llm_service() -> MagicMock:
    """Create a mock LLM service for testing."""
    service = MagicMock()
    service.generate.return_value = "Mock LLM response"
    return service
