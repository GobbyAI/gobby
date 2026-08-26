"""Pytest configuration and shared fixtures for Gobby tests."""

import logging
import os
import subprocess
import tempfile
from collections.abc import Callable, Generator, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Schema-per-worker Postgres fixtures (postgres_schema, postgres_canonical_seed,
# postgres_db). Tests that don't use them pay no runtime cost; the session
# fixtures only fire on first request.
pytest_plugins = ["tests.fixtures.postgres", "tests.review_coverage_helpers"]


def _ensure_isolated_bootstrap() -> None:
    """Write files_home (+ optional database_url) when GOBBY_HOME is empty.

    Pre-push points GOBBY_HOME at a fresh temp dir. Session-start and
    installer code call require_files_home() / load_bootstrap() and must
    not inherit the operator ~/.gobby. Do not overwrite a bootstrap a test
    already wrote.
    """
    raw_home = os.environ.get("GOBBY_HOME")
    if raw_home is None or not raw_home.strip():
        return
    home = Path(raw_home).expanduser()
    operator_home = Path.home() / ".gobby"
    if home.resolve(strict=False) == operator_home.resolve(strict=False):
        # The daemon exports its own home to every process it spawns. That is
        # the operator's state, not an isolated home: never provision it, even
        # when a sandbox hides its bootstrap (#20712).
        return
    bootstrap = home / "bootstrap.yaml"
    if bootstrap.exists():
        return
    files_home = home / "files"
    files_home.mkdir(parents=True, exist_ok=True)
    lines = [
        "datastore_mode: local\n",
        f"files_home: {files_home}\n",
    ]
    database_url = os.environ.get("GOBBY_POSTGRES_TEST_DSN") or os.environ.get("DATABASE_URL")
    if database_url:
        lines.append(f"database_url: {database_url}\n")
    bootstrap.write_text("".join(lines), encoding="utf-8")
    bootstrap.chmod(0o600)


_ensure_isolated_bootstrap()


@pytest.fixture(scope="session", autouse=True)
def _select_schema_contract_gdaemon() -> Iterator[None]:
    """Pin schema-contract gdaemon calls at the binary ``select_test_gdaemon`` chooses.

    The installed ``~/.gobby/bin`` binary by default — the same one the daemon uses,
    identity-checked against this checkout's pin so a mismatch fails loudly. Only
    ``GOBBY_TEST_GDAEMON=checkout`` routes applies at this checkout's ``target/debug``
    build, and that build must be newer than its crate sources. Session-scoped and
    autouse so the choice is active before the session-scoped postgres fixtures run
    their first schema apply/sweep, and uniform across the whole run.
    """
    from gobby.utils.native_bin import native_bin_name, resolve_native_bin
    from tests.fixtures.gdaemon_binary import select_test_gdaemon

    binary = select_test_gdaemon(
        Path(__file__).resolve().parents[1], os.environ, native_bin_name("gdaemon")
    )
    if binary is None:
        yield
        return
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "gobby.storage.schema_contract.resolve_native_bin",
        lambda name: str(binary) if name == "gdaemon" else resolve_native_bin(name),
    )
    yield
    monkeypatch.undo()


@pytest.fixture(autouse=True)
def _assert_postgres_pools_bounded() -> Iterator[None]:
    """Require each test to release every PostgreSQL pool it creates."""
    from gobby.storage.hub.postgres import _OPEN_DATABASES

    open_before = set(_OPEN_DATABASES)
    yield
    leaked = _OPEN_DATABASES - open_before
    assert not leaked, f"test left {len(leaked)} PostgresHubDatabase pool(s) open"


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register CLI flags for opt-in local-only test suites."""
    parser.addoption(
        "--run-sandbox",
        action="store_true",
        default=False,
        help="run sandbox compatibility tests that require local CLI binaries",
    )


def pytest_xdist_auto_num_workers(config: pytest.Config) -> int:
    """Bound auto workers to the local managed PostgreSQL lock capacity."""
    del config
    return min(os.cpu_count() or 1, 8)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Sort e2e tests to run last, reducing port collision risk with production daemon."""
    run_sandbox = bool(config.getoption("--run-sandbox"))
    skip_sandbox = pytest.mark.skip(reason="sandbox compatibility tests require --run-sandbox")
    non_e2e = []
    e2e = []
    for item in items:
        if "tests/integration/sandbox/" in str(item.fspath) and not run_sandbox:
            item.add_marker(skip_sandbox)
        if item.get_closest_marker("e2e") or "tests/e2e" in str(item.fspath):
            e2e.append(item)
        else:
            non_e2e.append(item)
    items[:] = non_e2e + e2e


if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.config.tasks import TaskValidationConfig
    from gobby.llm import LLMService
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.mcp import LocalMCPManager
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.sessions import SessionManager
    from gobby.tasks.validation import TaskValidator


@pytest.fixture
def make_task_validator() -> Callable[..., "TaskValidator"]:
    """Build a task validator with an isolated Hub database dependency."""
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.tasks.validation import TaskValidator

    def factory(
        config: "TaskValidationConfig",
        llm_service: "LLMService",
        **kwargs: Any,
    ) -> TaskValidator:
        return TaskValidator(config, llm_service, db=MagicMock(spec=HubDatabase), **kwargs)

    return factory


_GOBBY_LOGGER_NAMES = ("gobby", "gobby.hooks", "gobby.mcp.server", "gobby.mcp.client")


def _reset_process_global_state() -> None:
    """Reset process-owned registries that must not cross test boundaries."""
    import opentelemetry.metrics._internal as metrics_internal
    from opentelemetry import trace
    from opentelemetry.util._once import Once

    from gobby.agents import terminal_delivery
    from gobby.telemetry import providers as telemetry_providers

    api_tracer_provider = trace._TRACER_PROVIDER
    api_meter_provider = metrics_internal._METER_PROVIDER
    owned_tracer_provider = telemetry_providers._OWNED_TRACER_PROVIDER
    owned_meter_provider = telemetry_providers._OWNED_METER_PROVIDER

    telemetry_providers.shutdown_providers()
    for api_provider, owned_provider in (
        (api_tracer_provider, owned_tracer_provider),
        (api_meter_provider, owned_meter_provider),
    ):
        if api_provider is not None and api_provider is not owned_provider:
            shutdown = getattr(api_provider, "shutdown", None)
            if callable(shutdown):
                shutdown()

    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE = Once()
    metrics_internal._METER_PROVIDER = None
    metrics_internal._METER_PROVIDER_SET_ONCE = Once()

    terminal_delivery.detach_shielded_terminal_deliveries()
    terminal_delivery.reset_terminal_delivery_offload()
    terminal_delivery.reopen_terminal_delivery_admission()


@pytest.fixture(autouse=True)
def _restore_process_global_state() -> Generator[None]:
    """Isolate OpenTelemetry and terminal-delivery state around every test.

    OpenTelemetry provider registration uses process-global one-shot guards.
    Terminal delivery also owns process-global admission and in-flight task
    state. Tests that initialize telemetry or patch ``asyncio.create_task`` can
    otherwise poison every later runner lifecycle in the serial suite.
    """
    _reset_process_global_state()
    try:
        yield
    finally:
        _reset_process_global_state()


@pytest.fixture(autouse=True)
def _restore_gobby_logger_state() -> Generator[None]:
    """Snapshot and restore the gobby logger tree around every test.

    ``setup_otel_logging`` (and ``init_telemetry`` by extension) mutate the
    ``gobby`` logger and its sub-loggers: they set ``propagate=False``, attach
    rotating file + OTel handlers, and bump the level. Without teardown, those
    mutations leak across test modules — most visibly, ``propagate=False``
    silently hides warnings from pytest's ``caplog`` in downstream tests, which
    attaches its handler to root and relies on propagation. This must be
    suite-wide: any test that boots telemetry or a real daemon is a polluter.
    """
    snapshots = []
    for name in _GOBBY_LOGGER_NAMES:
        logger = logging.getLogger(name)
        snapshots.append((name, logger.level, logger.propagate, logger.handlers[:]))
    try:
        yield
    finally:
        for name, level, propagate, original_handlers in snapshots:
            logger = logging.getLogger(name)
            for handler in logger.handlers[:]:
                if handler not in original_handlers:
                    handler.close()
                    logger.removeHandler(handler)
            for handler in original_handlers:
                if handler not in logger.handlers:
                    logger.addHandler(handler)
            logger.setLevel(level)
            logger.propagate = propagate


@pytest.fixture
def temp_dir() -> Iterator[Path]:
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Return the repository root for tests that inspect checked-in files."""
    return Path(__file__).resolve().parents[1]


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


@pytest.fixture(scope="session")
def safe_gobby_home_dir() -> Iterator[Path]:
    """Session-scoped temp directory for safe Gobby home."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def temp_db(postgres_db: "HubDatabase") -> Iterator["HubDatabase"]:
    """Yield the PostgreSQL hub database used by storage tests."""
    yield postgres_db


class NonLocalHubDatabase:
    """HubDatabase proxy that is deliberately not a HubDatabase instance."""

    dialect = "postgres"

    def __init__(self, inner: "HubDatabase") -> None:
        self._inner = inner

    def transaction(self) -> Any:
        return self._inner.transaction()

    def transaction_immediate(self, lock: Any) -> Any:
        return self._inner.transaction_immediate(lock)

    def advisory_lock(self, lock: Any) -> Any:
        return self._inner.advisory_lock(lock)

    def execute(self, sql: str, params: Any = ()) -> Any:
        return self._inner.execute(sql, params)

    def executemany(self, sql: str, rows: Any) -> Any:
        return self._inner.executemany(sql, rows)

    def fetchone(self, sql: str, params: Any = ()) -> Any:
        return self._inner.fetchone(sql, params)

    def fetchall(self, sql: str, params: Any = ()) -> Any:
        return self._inner.fetchall(sql, params)

    def safe_update(self, table: str, values: Any, where: str, where_params: Any = ()) -> Any:
        return self._inner.safe_update(table, values, where, where_params)

    def apply_migrations(self) -> None:
        self._inner.apply_migrations()

    def close(self) -> None:
        self._inner.close()


@pytest.fixture
def non_local_hub_db(hub_db: "HubDatabase") -> NonLocalHubDatabase:
    """Wrap hub_db in an adapter that fails HubDatabase isinstance checks."""
    return NonLocalHubDatabase(hub_db)


@pytest.fixture(params=["postgres"])
def hub_db(
    postgres_db: "HubDatabase",
) -> Iterator["HubDatabase"]:
    """Yield a migrated PostgreSQL hub-database adapter.

    Tests that work through the ``HubDatabase`` protocol depend on this fixture
    instead of ``temp_db``. Depend directly on ``postgres_db`` so pytest applies
    the same setup order as ``temp_db(postgres_db)`` in cross-suite runs.
    """
    yield postgres_db


@pytest.fixture
def session_manager(temp_db: "HubDatabase") -> "SessionManager":
    """Create a session manager with temp database."""
    from gobby.storage.sessions import SessionManager

    return SessionManager(temp_db)


@pytest.fixture
def project_manager(temp_db: "HubDatabase") -> "LocalProjectManager":
    """Create a project manager with temp database."""
    from gobby.storage.projects import LocalProjectManager

    return LocalProjectManager(temp_db)


@pytest.fixture
def mcp_manager(temp_db: "HubDatabase") -> "LocalMCPManager":
    """Create an MCP manager with temp database."""
    from gobby.storage.mcp import LocalMCPManager

    return LocalMCPManager(temp_db)


@pytest.fixture
def fast_stop_hook_grace_window() -> Iterator[AsyncMock]:
    """Avoid real 5s shutdown delays in runner tests."""
    with patch(
        "gobby.runner_lifecycle._await_critical_stop_hook_grace_window",
        new=AsyncMock(),
    ) as mock_wait:
        yield mock_wait


@pytest.fixture
def mock_config() -> MagicMock:
    """Create a runner mock config with WebSocket disabled by default."""
    from tests.runner_helpers import apply_safe_runner_config_defaults

    config = MagicMock()
    config.daemon_port = 60887
    config.websocket = None
    return apply_safe_runner_config_defaults(config)


@pytest.fixture
def mock_config_with_websocket() -> MagicMock:
    """Create a runner mock config with WebSocket enabled."""
    from tests.runner_helpers import apply_safe_runner_config_defaults

    config = MagicMock()
    config.daemon_port = 60887
    config.websocket = MagicMock()
    config.websocket.enabled = True
    config.websocket.port = 60888
    config.websocket.ping_interval = 30
    config.websocket.ping_timeout = 10
    return apply_safe_runner_config_defaults(config)


def _init_git_repo(repo_path: Path) -> None:
    repo_path.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=repo_path,
        check=True,
        timeout=10,
    )
    (repo_path / "README.md").write_text("test project\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo_path, check=True, timeout=10)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Gobby Tests",
            "-c",
            "user.email=gobby-tests@example.com",
            "commit",
            "--no-gpg-sign",
            "-q",
            "-m",
            "initial",
        ],
        cwd=repo_path,
        check=True,
        timeout=10,
    )


@pytest.fixture
def sample_project(project_manager: "LocalProjectManager") -> dict[str, Any]:
    """Create a sample DB project for testing."""
    project = project_manager.create(
        name="test-project",
        github_url="https://github.com/test/test-project",
    )
    return project.to_dict()


@pytest.fixture
def sample_git_project(
    project_manager: "LocalProjectManager",
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> dict[str, Any]:
    """Create a sample project with a real git repo."""
    repo_path = tmp_path / "test-project"
    _init_git_repo(repo_path)
    project = project_manager.update(
        sample_project["id"],
        repo_path=str(repo_path),
        github_url="https://github.com/test/test-project",
    )
    assert project is not None
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

    Provides daemon_port, websocket.port, logging directory,
    and disables the UI.
    """
    config = MagicMock()
    config.daemon_port = 60887
    config.websocket.port = 60888
    temp_root = Path(tempfile.gettempdir())
    config.logging.dir = os.environ.get("GOBBY_LOGGING_DIR", str(temp_root))
    config.ui.enabled = False
    # A bare MagicMock attribute is truthy; leave the managed-SRT preflight off
    # so CLI start tests never probe the machine's SRT install (#21033).
    config.agent_sandbox.enabled = False
    config.web_chat_sandbox.enabled = False
    config.databases.falkordb.password = None
    return config


@pytest.fixture(autouse=True)
def protect_production_resources(
    request: pytest.FixtureRequest,
    temp_dir: Path,
    safe_gobby_home_dir: Path,
) -> Iterator[None]:
    """
    Defensive fixture to prevent tests from touching production resources.

    Forces all tests to use temporary paths for home and logging,
    unless explicitly opting out with @pytest.mark.no_config_protection.

    """
    if request.node.get_closest_marker("no_config_protection"):
        yield
        return

    import os

    # Use function-scoped temp_dir for logs (per-test isolation)
    safe_logs_dir = temp_dir / "logs"
    safe_logs_dir.mkdir(exist_ok=True)

    safe_hooks_dir = temp_dir / "hooks"
    safe_hooks_dir.mkdir(exist_ok=True)

    # Set environment variables as a first line of defense
    safe_config_file = safe_logs_dir / "config-test.yaml"
    env_vars = {
        "GOBBY_TEST_PROTECT": "1",  # Enable safety switch in app.py and cli/utils.py
        "GOBBY_HOME": str(safe_gobby_home_dir),
        "GOBBY_CONFIG_FILE": str(safe_config_file),  # Redirect config reads/writes
        "GOBBY_LOGGING_DIR": str(safe_logs_dir),
        "GOBBY_HOOKS_DIR": str(safe_hooks_dir),
    }

    with patch.dict(os.environ, env_vars):
        from gobby.config import app

        # Capture the REAL export_config_to_yaml to find rogue references
        try:
            _real_export_config_to_yaml: Any = app.export_config_to_yaml
        except AttributeError:
            _real_export_config_to_yaml = None

        def safe_export_config_to_yaml(config: Any, config_file: str | None = None) -> None:
            """Redirect config export to safe temp path during tests."""
            assert _real_export_config_to_yaml is not None
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
            _real_export_config_to_yaml(config, config_file=config_file)

        if _real_export_config_to_yaml is not None:
            p_export = patch(
                "gobby.config.app.export_config_to_yaml",
                side_effect=safe_export_config_to_yaml,
            )
            p_export.start()

        yield

        if _real_export_config_to_yaml is not None:
            p_export.stop()
