"""Pytest configuration and shared fixtures for Gobby tests."""

import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from filelock import FileLock


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Sort e2e tests to run last, reducing port collision risk with production daemon."""
    non_e2e = []
    e2e = []
    for item in items:
        if item.get_closest_marker("e2e") or "tests/e2e" in str(item.fspath):
            e2e.append(item)
        else:
            non_e2e.append(item)
    items[:] = non_e2e + e2e


if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.storage.database import LocalDatabase
    from gobby.storage.mcp import LocalMCPManager
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.sessions import LocalSessionManager


@pytest.fixture
def temp_dir() -> Iterator[Path]:
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def enable_log_propagation() -> Iterator[None]:
    """Enable log propagation for caplog tests.

    The gobby package logger has propagate=False to avoid duplicate logs in production.
    This fixture temporarily enables propagation so caplog can capture logs.
    """
    import logging

    gobby_logger = logging.getLogger("gobby")
    original_propagate = gobby_logger.propagate
    gobby_logger.propagate = True
    yield
    gobby_logger.propagate = original_propagate


@pytest.fixture(scope="session")
def safe_db_dir() -> Iterator[Path]:
    """Session-scoped temp directory for safe database.

    This directory persists for the entire test session to avoid the race condition
    where the database file is deleted before all tests finish using it.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def temp_db(temp_dir: Path) -> Iterator["LocalDatabase"]:
    """Create a temporary database for testing."""
    from gobby.storage.database import LocalDatabase
    from gobby.storage.migrations import run_migrations

    db_path = temp_dir / "test.db"
    db = LocalDatabase(db_path)
    run_migrations(db)
    yield db
    db.close()


@pytest.fixture
def session_manager(temp_db: "LocalDatabase") -> "LocalSessionManager":
    """Create a session manager with temp database."""
    from gobby.storage.sessions import LocalSessionManager

    return LocalSessionManager(temp_db)


@pytest.fixture
def project_manager(temp_db: "LocalDatabase") -> "LocalProjectManager":
    """Create a project manager with temp database."""
    from gobby.storage.projects import LocalProjectManager

    return LocalProjectManager(temp_db)


@pytest.fixture
def mcp_manager(temp_db: "LocalDatabase") -> "LocalMCPManager":
    """Create an MCP manager with temp database."""
    from gobby.storage.mcp import LocalMCPManager

    return LocalMCPManager(temp_db)


@pytest.fixture
def sample_project(project_manager: "LocalProjectManager") -> dict:
    """Create a sample project for testing."""
    project = project_manager.create(
        name="test-project",
        repo_path="/tmp/test-project",
        github_url="https://github.com/test/test-project",
    )
    return project.to_dict()


@pytest.fixture
def default_config() -> "DaemonConfig":
    """Create a default DaemonConfig for testing."""
    from gobby.config.app import DaemonConfig

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


@pytest.fixture
def mock_daemon_config() -> "MagicMock":
    """Create a mock daemon configuration for CLI tests.

    Provides daemon_port, websocket.port, logging paths,
    and disables watchdog and UI.
    """
    config = MagicMock()
    config.daemon_port = 60887
    config.websocket.port = 60888
    config.logging.client = "~/.gobby/logs/client.log"
    config.logging.client_error = "~/.gobby/logs/client_error.log"
    config.watchdog.enabled = False
    config.ui.enabled = False
    config.memory.neo4j_url = None
    config.memory.neo4j_auth = None
    return config


@pytest.fixture(autouse=True)
def protect_production_resources(
    request: pytest.FixtureRequest, temp_dir: Path, safe_db_dir: Path
) -> Iterator[None]:
    """
    Defensive fixture to prevent tests from touching production resources.

    Forces all tests to use temporary paths for database and logging,
    unless explicitly opting out with @pytest.mark.no_config_protection.

    Uses a session-scoped directory for the database to avoid race conditions
    where the database file gets deleted before all tests finish using it.
    """
    if request.node.get_closest_marker("no_config_protection"):
        yield
        return

    import os

    from gobby.config.app import DaemonConfig

    # Use session-scoped directory for database (persists for entire test session)
    # Use function-scoped temp_dir for logs (per-test isolation)
    safe_db_path = safe_db_dir / "test-safe.db"
    safe_logs_dir = temp_dir / "logs"
    safe_logs_dir.mkdir(exist_ok=True)

    # Run migrations on safe database - this is CRITICAL!
    # Code that calls LocalDatabase() without arguments will use this path via GOBBY_DATABASE_PATH.
    # Without migrations, queries will fail with "file is not a database" errors.
    # Only run migrations if the database doesn't exist yet (session-scoped, reused across tests).
    from gobby.storage.database import LocalDatabase
    from gobby.storage.migrations import run_migrations

    # Use file lock to prevent TOCTOU race condition during parallel test execution.
    # Without this, multiple pytest workers can simultaneously check exists() -> False,
    # then race to create the database, causing "file is not a database" errors.
    lock_path = safe_db_dir / "test-safe.db.lock"
    with FileLock(lock_path, timeout=60):
        if not safe_db_path.exists():
            safe_db = LocalDatabase(safe_db_path)
            run_migrations(safe_db)
            safe_db.close()

    safe_log_client = safe_logs_dir / "gobby.log"
    safe_log_error = safe_logs_dir / "gobby-error.log"
    safe_log_mcp_server = safe_logs_dir / "mcp-server.log"
    safe_log_mcp_client = safe_logs_dir / "mcp-client.log"
    safe_hooks_dir = temp_dir / "hooks"
    safe_hooks_dir.mkdir(exist_ok=True)

    # Set environment variables as a first line of defense
    safe_config_file = safe_logs_dir / "config-test.yaml"
    env_vars = {
        "GOBBY_TEST_PROTECT": "1",  # Enable safety switch in app.py, database.py, and cli/utils.py
        "GOBBY_DATABASE_PATH": str(safe_db_path),
        "GOBBY_CONFIG_FILE": str(safe_config_file),  # Redirect config reads/writes
        "GOBBY_LOGGING_CLIENT": str(safe_log_client),
        "GOBBY_LOGGING_CLIENT_ERROR": str(safe_log_error),
        "GOBBY_LOGGING_MCP_SERVER": str(safe_log_mcp_server),
        "GOBBY_LOGGING_MCP_CLIENT": str(safe_log_mcp_client),
        "GOBBY_HOOKS_DIR": str(safe_hooks_dir),
    }

    with patch.dict(os.environ, env_vars):
        # Patch load_config to return a safe config
        # We need to use a side_effect to allow partial loading if needed,
        # but for most tests returning a safe config object is best.
        # However, many tests mock load_config themselves.
        # We'll use a wrapper that returns our safe config unless arguments suggest otherwise.

        try:
            from gobby.config import app

            # Capture the REAL function object before we patch anything
            # We need this identity to find other references to it
            _real_load_config = app.load_config
        except ImportError:
            _real_load_config = None

        def safe_load_config(*args, **kwargs):
            # If creating default, let it happen but in safe location if possible
            # But simpler is to just return a safe config object
            config = DaemonConfig(
                database_path=str(safe_db_path),
                logging={
                    "client": str(safe_log_client),
                    "client_error": str(safe_log_error),
                    "mcp_server": str(safe_log_mcp_server),
                    "mcp_client": str(safe_log_mcp_client),
                },
            )
            # Apply overrides if present (logic from real load_config)
            if "cli_overrides" in kwargs and kwargs["cli_overrides"]:
                from gobby.config.app import apply_cli_overrides

                start_dict = config.model_dump(exclude_none=True)
                final_dict = apply_cli_overrides(start_dict, kwargs["cli_overrides"])
                config = DaemonConfig(**final_dict)
            return config

        # PATCHING STRATEGY:
        # standard patch() only patches the name in the target module.
        # But if other modules (like gobby.runner) have already done "from gobby.config.app import load_config",
        # they have a reference to the OLD function object.
        # We must find ALL references to the old function and patch them too.

        # Capture the REAL save_config to find rogue references
        try:
            _real_save_config: Any = app.save_config
        except AttributeError:
            _real_save_config = None

        def safe_save_config(config: Any, config_file: str | None = None) -> None:
            """Redirect save_config to safe temp path during tests."""
            assert _real_save_config is not None
            if config_file is None:
                config_file = str(safe_config_file)
            else:
                # Redirect production paths to safe location
                resolved = Path(config_file).expanduser().resolve()
                real_gobby_home = Path("~/.gobby").expanduser().resolve()
                try:
                    if resolved.is_relative_to(real_gobby_home):
                        config_file = str(safe_config_file)
                except (ValueError, OSError):
                    pass
            _real_save_config(config, config_file=config_file)

        # 1. Standard patch for the definitions (covers future imports)
        p = patch("gobby.config.app.load_config", side_effect=safe_load_config)
        p.start()
        if _real_save_config is not None:
            p_save = patch("gobby.config.app.save_config", side_effect=safe_save_config)
            p_save.start()

        # 2. Patch known top-level importers of load_config / save_config.
        #
        # Only modules with a top-level `from gobby.config.app import load_config`
        # hold a direct reference that patch() on the definition module won't reach.
        # Lazy (in-function) imports resolve at call time and get the patched version.
        #
        # To update this list: grep for top-level `from gobby.config.app import load_config`
        # in src/ (exclude lines inside function bodies).  Also include gobby.config since
        # its __init__.py re-exports both load_config and save_config.
        patched_modules = []
        import sys

        _KNOWN_CONFIG_IMPORTERS = [
            "gobby.config",  # __init__.py re-exports load_config and save_config
            "gobby.runner",
            "gobby.cli",  # cli/__init__.py
            "gobby.cli.utils",
            "gobby.cli.tasks._utils",
            "gobby.mcp_proxy.stdio",
            "gobby.watchdog",
        ]

        rogue_replacements: dict[int, tuple[Any, Any]] = {}
        if _real_load_config:
            rogue_replacements[id(_real_load_config)] = (safe_load_config, _real_load_config)
        if _real_save_config:
            rogue_replacements[id(_real_save_config)] = (safe_save_config, _real_save_config)

        if rogue_replacements:
            for mod_name in _KNOWN_CONFIG_IMPORTERS:
                mod = sys.modules.get(mod_name)
                if mod is None:
                    continue

                updates = {}
                for attr_name, attr_val in mod.__dict__.items():
                    replacement = rogue_replacements.get(id(attr_val))
                    if replacement:
                        updates[attr_name] = replacement  # (safe_fn, real_fn)

                if updates:
                    for attr_name, (safe_fn, _real_fn) in updates.items():
                        setattr(mod, attr_name, safe_fn)
                    patched_modules.append((mod, updates))

        yield

        # Restore everything
        p.stop()
        if _real_save_config is not None:
            p_save.stop()
        for mod, updates in patched_modules:
            for attr_name, (_safe_fn, real_fn) in updates.items():
                if real_fn is not None:
                    setattr(mod, attr_name, real_fn)
                else:
                    try:
                        delattr(mod, attr_name)
                    except AttributeError:
                        pass
