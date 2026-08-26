"""
E2E test configuration and fixtures for Gobby daemon.

Provides fixtures for:
- Spawning isolated daemon processes
- Waiting for daemon readiness
- Capturing daemon logs
- Cleaning up orphan processes
- CLI event simulation
- MCP client connections
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import AsyncGenerator, Callable, Generator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio
import yaml

from gobby.utils.session_context import AGENT_RUN_ID_HEADER

# Mark all tests in this directory as e2e tests
pytestmark = pytest.mark.e2e


class ValidationLLMServer(ThreadingHTTPServer):
    """Deterministic local provider for bounded task-close criteria reviews."""

    validation_calls: int
    requests: list[dict[str, Any]]


class _ValidationLLMHandler(BaseHTTPRequestHandler):
    server: ValidationLLMServer

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        self.server.validation_calls += 1
        self.server.requests.append(cast(dict[str, Any], request))
        verdict = {
            "status": "valid",
            "criteria": [{"index": 1, "satisfied": True, "gap": None}],
            "feedback": "The supplied evidence coherently satisfies the criterion.",
        }
        response = {
            "id": "chatcmpl-e2e",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "e2e-validation",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(verdict),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        }
        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        return


@pytest.fixture
def validation_llm_server() -> Generator[ValidationLLMServer]:
    server = ValidationLLMServer(("127.0.0.1", 0), _ValidationLLMHandler)
    server.validation_calls = 0
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def configure_task_close_validation(
    e2e_config: tuple[Path, int, int],
    validation_llm_server: ValidationLLMServer,
    postgres_db: Any,
) -> None:
    """Enable close validation against the deterministic local provider."""
    from gobby.prompts.sync import sync_bundled_prompts

    sync_bundled_prompts(postgres_db)
    from gobby.storage.config_mutations import ConfigMutations, ConfigPatch

    endpoint = {
        "protocol": "openai-compatible",
        "wire_api": "chat-completions",
        "api_base": f"http://127.0.0.1:{validation_llm_server.server_port}/v1",
        "model": "e2e-validation",
    }
    mutations = ConfigMutations(postgres_db)
    mutations.patch_internal(
        expected_revision=mutations.repository.current_revision(),
        patch=ConfigPatch(
            values={
                "gobby_tasks.validation.enabled": True,
                "gobby_tasks.validation.candidates": ["endpoint:e2e/e2e-validation"],
                "ai.generation.timeout_seconds": 15,
                "ai.generation.candidate_timeout_seconds": 5,
                "ai.generation.cli_candidate_timeout_seconds": 5,
                "ai.generation.endpoints.e2e.protocol": endpoint["protocol"],
                "ai.generation.endpoints.e2e.wire_api": endpoint["wire_api"],
                "ai.generation.endpoints.e2e.api_base": endpoint["api_base"],
                "ai.generation.endpoints.e2e.model": endpoint["model"],
            }
        ),
        source="e2e-fixture",
    )
    config_path, _http_port, _ws_port = e2e_config
    config = cast(dict[str, Any], yaml.safe_load(config_path.read_text()))
    validation = config["gobby_tasks"]["validation"]
    validation["enabled"] = True
    validation["candidates"] = ["endpoint:e2e/e2e-validation"]
    config["ai"] = {
        "generation": {
            "timeout_seconds": 15,
            "candidate_timeout_seconds": 5,
            "cli_candidate_timeout_seconds": 5,
            "endpoints": {"e2e": endpoint},
        }
    }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))


@dataclass
class DaemonInstance:
    """Represents a running daemon instance."""

    process: subprocess.Popen[bytes]
    pid: int
    http_port: int
    ws_port: int
    project_dir: Path
    gobby_dir: Path
    log_file: Path
    error_log_file: Path
    db_path: Path
    config_path: Path
    command: list[str]
    env: dict[str, str]

    @property
    def http_url(self) -> str:
        """HTTP base URL."""
        return f"http://localhost:{self.http_port}"

    @property
    def ws_url(self) -> str:
        """WebSocket URL."""
        return f"ws://localhost:{self.ws_port}"

    @property
    def gobby_home(self) -> Path:
        """Isolated daemon home containing bootstrap state and credentials."""
        return self.config_path.parent

    def is_alive(self) -> bool:
        """Check if daemon process is still running."""
        return self.process.poll() is None

    def read_logs(self) -> str:
        """Read stdout logs."""
        if self.log_file.exists():
            return self.log_file.read_text()
        return ""

    def read_error_logs(self) -> str:
        """Read stderr logs."""
        if self.error_log_file.exists():
            return self.error_log_file.read_text()
        return ""

    def stop(self) -> None:
        """Stop the daemon and wait for its health endpoint to disappear."""
        if self.is_alive():
            terminate_process_tree(self.pid)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if daemon_health_unavailable(self.http_port):
                return
            time.sleep(0.05)
        pytest.fail(f"Daemon health endpoint on port {self.http_port} remained available")

    def restart(self) -> None:
        """Restart the daemon with the fixture's original process configuration."""
        if self.is_alive():
            raise RuntimeError("Cannot restart a running daemon")

        with self.log_file.open("ab") as log_f, self.error_log_file.open("ab") as err_f:
            process = subprocess.Popen(
                self.command,
                stdout=log_f,
                stderr=err_f,
                stdin=subprocess.DEVNULL,
                cwd=str(self.project_dir),
                env=self.env,
                start_new_session=True,
            )

        self.process = process
        self.pid = process.pid

        time.sleep(0.5)
        if process.poll() is not None:
            pytest.fail(
                f"Daemon subprocess died immediately with exit code {process.poll()}.\n"
                f"Logs:\n{self.read_logs()}\nError output:\n{self.read_error_logs()}"
            )
        if not wait_for_daemon_health(self.http_port, timeout=30.0):
            terminate_process_tree(process.pid)
            pytest.fail(
                f"Daemon failed to restart within timeout.\n"
                f"Logs:\n{self.read_logs()}\nError logs:\n{self.read_error_logs()}"
            )
        if not wait_for_port(self.ws_port, timeout=10.0):
            terminate_process_tree(process.pid)
            pytest.fail(
                f"Daemon WebSocket port {self.ws_port} did not become ready within timeout.\n"
                f"Logs:\n{self.read_logs()}\nError logs:\n{self.read_error_logs()}"
            )


def prepare_daemon_env(
    base_env: dict[str, str] | None = None,
    *,
    home_dir: str | Path | None = None,
) -> dict[str, str]:
    """Prepare environment variables for spawning a daemon subprocess.

    This handles the critical setup that's easy to miss when manually spawning daemons:
    1. Sets PYTHONPATH to include the src directory
    2. Removes GOBBY_DATABASE_PATH so daemon uses its config file's database_url
    3. Clears LLM API keys to avoid external calls
    4. Overrides HOME to isolate the daemon from the real ~/.gobby/

    Args:
        base_env: Base environment dict to modify. If None, copies os.environ.
        home_dir: Override HOME to this directory. When set, all paths using
            ``~/.gobby`` (database, logs, qdrant, session summaries, MCP config)
            resolve inside the test temp dir instead of the real home.

    Returns:
        Environment dict ready for subprocess.Popen
    """
    env = dict(base_env) if base_env is not None else os.environ.copy()

    # Set PYTHONPATH so the daemon can import gobby modules
    root_dir = Path(__file__).parent.parent.parent
    src_dir = root_dir / "src"
    current_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{src_dir}:{current_pythonpath}" if current_pythonpath else str(src_dir)

    # Remove test-process-specific path overrides so the daemon uses its own
    # isolated config/DB. GOBBY_TEST_PROTECT is forced here: it is the safety
    # guard that prevents stop_daemon / kill_all_gobby_daemons /
    # stop_daemon_process / get_daemon_pid in the spawned daemon (and any
    # subprocesses it forks: agents, hooks, helper CLIs) from reaching the
    # user's real daemon via system-wide psutil discovery.
    env["GOBBY_TEST_PROTECT"] = "1"
    env.pop("GOBBY_DATABASE_PATH", None)
    env.pop("GOBBY_CONFIG_FILE", None)

    # Disable any LLM providers to avoid external calls. The memory-helper
    # live smoke is explicitly opt-in and needs the real provider credentials.
    if env.get("GOBBY_LIVE_MEMORY_HELPER_E2E") != "1":
        env["ANTHROPIC_API_KEY"] = ""
        env["OPENAI_API_KEY"] = ""
        env["GEMINI_API_KEY"] = ""

    # Override HOME so that ~/.gobby resolves to <temp>/.gobby instead of
    # the user's real home directory. This is the single most effective
    # isolation measure: it catches every expanduser() call in the daemon.
    if home_dir is not None:
        home_path = Path(home_dir)
        env["HOME"] = str(home_path)

    return env


def find_free_port(max_retries: int = 20) -> int:
    """Find an available port that won't collide with any running daemon.

    Binds to 0.0.0.0 so the OS detects conflicts with a production daemon
    also bound to 0.0.0.0. Restricts to port range 30000-40000, well away
    from production's 60887-60889. Also excludes known gobby ports as
    defense-in-depth.
    """
    EXCLUDED_PORTS = {60887, 60888, 60889}
    PORT_MIN, PORT_MAX = 30000, 40000
    for _attempt in range(max_retries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("0.0.0.0", 0))
            port = int(s.getsockname()[1])

        if port in EXCLUDED_PORTS or not (PORT_MIN <= port <= PORT_MAX):
            continue

        # Verify port is actually available
        time.sleep(0.05)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as v:
                v.bind(("0.0.0.0", port))
            return port
        except OSError:
            continue

    # Fallback: explicitly pick from range
    import random

    for _ in range(max_retries):
        port = random.randint(PORT_MIN, PORT_MAX)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as v:
                v.bind(("0.0.0.0", port))
            return port
        except OSError:
            continue
    raise RuntimeError("Could not find free port for e2e test")


def _postgres_url_for_schema(database_url: str, schema: str) -> str:
    separator = "&" if "?" in database_url else "?"
    return f"{database_url}{separator}options=-csearch_path%3D{schema}"


def _seed_e2e_runtime_state(postgres_db: Any, project_dir: Path) -> None:
    """Seed PostgreSQL-owned runtime config and the synthetic E2E project."""
    from gobby.storage.config_mutations import ConfigMutations, ConfigPatch

    mutations = ConfigMutations(postgres_db)
    mutations.patch_internal(
        expected_revision=mutations.repository.current_revision(),
        patch=ConfigPatch(
            values={
                "test_mode": True,
                "memory.dream.enabled": False,
                "gobby_tasks.expansion.enabled": False,
                "gobby_tasks.validation.enabled": False,
                "code_index.enabled": False,
            }
        ),
        source="e2e-fixture",
    )
    postgres_db.execute(
        """
        INSERT INTO projects (id, name, repo_path, created_at, updated_at)
        VALUES (%s, %s, %s, NOW(), NOW())
        ON CONFLICT (id) DO UPDATE
        SET name = EXCLUDED.name,
            repo_path = EXCLUDED.repo_path,
            updated_at = EXCLUDED.updated_at
        """,
        (
            "00000000-0000-0000-0000-000000000e2e",
            "E2E Test Project",
            str(project_dir),
        ),
    )


def wait_for_port(port: int, timeout: float = 10.0) -> bool:
    """Wait for a port to become available for connection."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection(("localhost", port), timeout=0.5):
                return True
        except (ConnectionRefusedError, OSError, TimeoutError):
            time.sleep(0.1)
    return False


def wait_for_daemon_health(port: int, timeout: float = 30.0) -> bool:
    """Wait for the daemon's public authentication status route."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            response = httpx.get(
                f"http://localhost:{port}/api/auth/status",
                timeout=2.0,
            )
            if response.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadTimeout, httpx.ReadError):
            pass
        # Sleep on every non-ready iteration so polling does not steal cycles
        # from the daemon we are waiting on.
        time.sleep(0.5)
    return False


def daemon_health_unavailable(port: int) -> bool:
    """Return true when the daemon health endpoint is no longer reachable."""
    try:
        response = httpx.get(
            f"http://localhost:{port}/api/admin/startup-progress",
            timeout=0.2,
        )
        return response.status_code != 200
    except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadTimeout, httpx.ReadError):
        return True


def terminate_process_tree(pid: int, timeout: float = 5.0) -> None:
    """Terminate a process and all its children."""
    import psutil

    try:
        parent = psutil.Process(pid)
        try:
            children = parent.children(recursive=True)
        except (psutil.AccessDenied, PermissionError):
            # macOS sandboxing can block full process enumeration. E2E daemons
            # start in their own session, so the process group is a safe fallback.
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                parent.wait(timeout=timeout / 2)
            except psutil.TimeoutExpired:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    parent.wait(timeout=1.0)
                except psutil.NoSuchProcess:
                    pass
            except psutil.NoSuchProcess:
                pass
            return

        # Terminate children first
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass

        # Wait for children
        gone, alive = psutil.wait_procs(children, timeout=timeout / 2)

        # Force kill remaining children
        for p in alive:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass

        # Terminate parent
        try:
            parent.terminate()
            parent.wait(timeout=timeout / 2)
        except psutil.TimeoutExpired:
            parent.kill()
            parent.wait(timeout=1.0)
        except psutil.NoSuchProcess:
            pass

    except psutil.NoSuchProcess:
        pass


@pytest.fixture(scope="function")
def e2e_project_dir() -> Generator[Path]:
    """Create an isolated project directory for E2E tests.

    ``ignore_cleanup_errors=True`` protects against ENOTEMPTY on macOS when a
    daemon subprocess (or one of its helpers — gcode, CLI tools, etc.) still
    holds file descriptors inside the tree at teardown. terminate_process_tree
    gives children a few seconds to exit, but the kernel may still report a
    directory as non-empty briefly after. Without this flag, the whole test
    errors at teardown even though the assertions already passed.
    """
    with tempfile.TemporaryDirectory(prefix="gobby_e2e_", ignore_cleanup_errors=True) as tmpdir:
        project_dir = Path(tmpdir).resolve()
        gobby_dir = project_dir / ".gobby"
        gobby_dir.mkdir(parents=True, exist_ok=True)

        # Initialize git repository for clone/worktree tests
        subprocess.run(
            ["git", "init"],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
        # Create initial commit (needed for worktree/clone operations)
        (project_dir / "README.md").write_text("# E2E Test Project\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )

        # Create project.json
        project_json = gobby_dir / "project.json"
        project_json.write_text(
            json.dumps(
                {
                    "id": "00000000-0000-0000-0000-000000000e2e",
                    "name": "E2E Test Project",
                    "repo_path": str(project_dir),
                }
            )
        )

        # Copy shared pipelines and agents for spawn_agent tests
        from gobby.agents.sync import get_bundled_agents_path
        from gobby.workflows.sync_pipelines import get_bundled_pipelines_path

        shared_pipelines = get_bundled_pipelines_path()
        if shared_pipelines.exists():
            target_workflows = gobby_dir / "workflows"
            target_workflows.mkdir(parents=True, exist_ok=True)
            for wf_file in shared_pipelines.glob("*.yaml"):
                shutil.copy2(wf_file, target_workflows / wf_file.name)

        shared_agents = get_bundled_agents_path()
        if shared_agents.exists():
            target_agents = gobby_dir / "agents"
            target_agents.mkdir(parents=True, exist_ok=True)
            for agent_file in shared_agents.glob("*.yaml"):
                shutil.copy2(agent_file, target_agents / agent_file.name)

        yield project_dir


@pytest.fixture(scope="function")
def e2e_config(
    e2e_project_dir: Path,
    postgres_database_url: str,
    postgres_schema: str,
    postgres_db: Any,
) -> Generator[tuple[Path, int, int]]:
    """Create an isolated config file with unique ports."""
    _ = postgres_db  # Fixture side effect: migrated and reset isolated Postgres schema.

    http_port = find_free_port()
    ws_port = find_free_port()

    gobby_home = e2e_project_dir / ".gobby-home"
    gobby_home.mkdir(parents=True, exist_ok=True)

    # Pin the daemon's machine identity to the synthetic id the e2e suite
    # registers sessions with. Machine-ownership enforcement (c55fccf31)
    # rejects explicit foreign machine ids at registration, and the daemon
    # subprocess resolves identity from this home — test-side patches can't
    # reach it.
    (gobby_home / "machine_id").write_text("21000000-0000-4000-8000-000000000002")

    config_path = gobby_home / "config.yaml"
    db_path = gobby_home / "hub-postgres.db"
    log_dir = gobby_home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    postgres_url = _postgres_url_for_schema(postgres_database_url, postgres_schema)

    # Runtime configuration is PostgreSQL-owned. The legacy config.yaml below
    # remains input coverage for bootstrap-path resolution only.
    _seed_e2e_runtime_state(postgres_db, e2e_project_dir)

    config_content = f"""
daemon_port: {http_port}
test_mode: true
database_url: "{db_path}"

websocket:
  enabled: true
  port: {ws_port}
  ping_interval: 30
  ping_timeout: 10

logging:
  client: "{log_dir}/client.log"
  client_error: "{log_dir}/client_error.log"

session_lifecycle:
  idle_timeout_minutes: 60
  max_sessions_per_machine: 10
  cleanup_interval_minutes: 5

gobby_tasks:
  expansion:
    enabled: false
  validation:
    enabled: false

code_index:
  enabled: false

embeddings:
  api_base: "http://127.0.0.1:11434/v1"

memory:
  dream:
    enabled: false

session_summary:
  summary_file_path: "{gobby_home}/session_summaries"
"""

    config_path.write_text(config_content)

    # Write bootstrap.yaml so the runner picks up ports and db_path
    # (load_config Phase 1 reads bootstrap.yaml, not config.yaml)
    bootstrap_path = gobby_home / "bootstrap.yaml"
    files_home = gobby_home / "files"
    files_home.mkdir(exist_ok=True)
    bootstrap_content = f"""
hub_backend: postgres
database_url: {postgres_url}
daemon_port: {http_port}
bind_host: localhost
websocket_port: {ws_port}
files_home: {files_home}
"""
    bootstrap_path.write_text(bootstrap_content)
    bootstrap_path.chmod(0o600)

    yield config_path, http_port, ws_port


@pytest.fixture(scope="function")
def e2e_pre_daemon_setup() -> None:
    """Optional per-test setup that must run after DB reset and before daemon start."""


@pytest.fixture(scope="function")
def daemon_instance(
    e2e_project_dir: Path,
    e2e_config: tuple[Path, int, int],
    e2e_pre_daemon_setup: None,
) -> Generator[DaemonInstance]:
    """
    Spawn an isolated daemon instance for E2E testing.

    Yields a DaemonInstance with running daemon, then cleans up on teardown.
    """
    _ = e2e_pre_daemon_setup
    config_path, http_port, ws_port = e2e_config
    gobby_home = config_path.parent
    log_dir = gobby_home / "logs"

    log_file = log_dir / "daemon.log"
    error_log_file = log_dir / "daemon_error.log"

    # Use prepare_daemon_env for consistent isolation (PYTHONPATH, API keys,
    # env cleanup, and HOME override so ~/.gobby resolves inside the temp dir)
    env = prepare_daemon_env(home_dir=gobby_home)
    env["GOBBY_CONFIG"] = str(config_path)
    env["GOBBY_HOME"] = str(gobby_home)

    command = [sys.executable, "-m", "gobby.runner", "--config", str(config_path)]

    # Start daemon process
    with open(log_file, "w") as log_f, open(error_log_file, "w") as err_f:
        process = subprocess.Popen(
            command,
            stdout=log_f,
            stderr=err_f,
            stdin=subprocess.DEVNULL,
            cwd=str(e2e_project_dir),
            env=env,
            start_new_session=True,
        )

    # Brief delay to catch immediate failures
    time.sleep(0.5)
    if process.poll() is not None:
        error_logs = error_log_file.read_text() if error_log_file.exists() else ""
        logs = log_file.read_text() if log_file.exists() else ""
        pytest.fail(
            f"Daemon subprocess died immediately with exit code {process.poll()}.\n"
            f"Logs:\n{logs}\nError output:\n{error_logs}"
        )

    instance = DaemonInstance(
        process=process,
        pid=process.pid,
        http_port=http_port,
        ws_port=ws_port,
        project_dir=e2e_project_dir,
        gobby_dir=e2e_project_dir / ".gobby",
        log_file=log_file,
        error_log_file=error_log_file,
        db_path=gobby_home / "hub-postgres.db",
        config_path=config_path,
        command=command,
        env=env,
    )

    # Wait for daemon to be healthy (longer timeout for when running with full test suite)
    if not wait_for_daemon_health(http_port, timeout=30.0):
        # Daemon failed to start - capture logs for debugging
        logs = instance.read_logs()
        error_logs = instance.read_error_logs()
        exit_code = process.poll()
        terminate_process_tree(process.pid)
        extra_info = f"\nProcess exited with code: {exit_code}" if exit_code is not None else ""
        pytest.fail(
            f"Daemon failed to start within timeout.{extra_info}\n"
            f"Logs:\n{logs}\nError logs:\n{error_logs}"
        )

    # HTTP health check passes as soon as /api/admin/status responds, but the
    # WebSocket server comes up on a separate port and can lag by a few hundred
    # ms. Tests like test_daemon_listens_on_configured_ports race against that
    # gap — probe the WS port explicitly so it's listening before yield.
    if not wait_for_port(ws_port, timeout=10.0):
        logs = instance.read_logs()
        error_logs = instance.read_error_logs()
        terminate_process_tree(process.pid)
        pytest.fail(
            f"Daemon WebSocket port {ws_port} did not become ready within timeout.\n"
            f"Logs:\n{logs}\nError logs:\n{error_logs}"
        )

    yield instance

    # Cleanup
    if instance.is_alive():
        terminate_process_tree(instance.pid)


@pytest_asyncio.fixture
async def async_daemon_instance(
    daemon_instance: DaemonInstance,
) -> AsyncGenerator[DaemonInstance]:
    """Async-compatible daemon instance fixture."""
    yield daemon_instance


@pytest.fixture(scope="function")
def daemon_client(daemon_instance: DaemonInstance) -> Generator[httpx.Client]:
    """HTTP client configured for daemon instance."""
    with authenticated_daemon_client(daemon_instance) as client:
        yield client


@pytest_asyncio.fixture
async def async_daemon_client(
    daemon_instance: DaemonInstance,
) -> AsyncGenerator[httpx.AsyncClient]:
    """Async HTTP client configured for daemon instance."""
    async with authenticated_async_daemon_client(daemon_instance) as client:
        yield client


def daemon_token(gobby_home: Path) -> str:
    """Read the isolated daemon's CLI bearer token."""
    token = (gobby_home / "local_cli_token").read_text().strip()
    if not token:
        raise RuntimeError(f"Daemon token is empty: {gobby_home / 'local_cli_token'}")
    return token


def daemon_auth_headers(gobby_home: Path) -> dict[str, str]:
    """Build bearer headers for an isolated daemon home."""
    return {"Authorization": f"Bearer {daemon_token(gobby_home)}"}


def authenticated_daemon_client(
    daemon_instance: DaemonInstance,
    *,
    timeout: float = 10.0,
) -> httpx.Client:
    """Create a bearer-authenticated sync client for an isolated daemon."""
    return httpx.Client(
        base_url=daemon_instance.http_url,
        headers=daemon_auth_headers(daemon_instance.gobby_home),
        timeout=timeout,
    )


def authenticated_daemon_client_for_home(
    base_url: str,
    gobby_home: Path,
    *,
    timeout: float = 10.0,
) -> httpx.Client:
    """Create a bearer-authenticated client for a manually spawned daemon."""
    return httpx.Client(
        base_url=base_url,
        headers=daemon_auth_headers(gobby_home),
        timeout=timeout,
    )


def authenticated_daemon_request(
    method: str,
    url: str,
    gobby_home: Path,
    **kwargs: Any,
) -> httpx.Response:
    """Send one bearer-authenticated request to a manually spawned daemon."""
    headers = dict(kwargs.pop("headers", {}))
    headers.update(daemon_auth_headers(gobby_home))
    return httpx.request(method, url, headers=headers, **kwargs)


def authenticated_async_daemon_client(
    daemon_instance: DaemonInstance,
    *,
    timeout: float = 10.0,
) -> httpx.AsyncClient:
    """Create a bearer-authenticated async client for an isolated daemon."""
    return httpx.AsyncClient(
        base_url=daemon_instance.http_url,
        headers=daemon_auth_headers(daemon_instance.gobby_home),
        timeout=timeout,
    )


# --- CLI Event Helpers ---


class CLIEventSimulator:
    """Helper for simulating CLI hook events and session registration."""

    def __init__(self, daemon_url: str, token: str):
        self.daemon_url = daemon_url
        self.client = httpx.Client(
            base_url=daemon_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )

    def close(self) -> None:
        """Close the HTTP client."""
        self.client.close()

    def _hook_envelope(self, **payload: Any) -> dict[str, Any]:
        envelope = {
            "schema_version": 1,
            "enqueued_at": "2026-04-16T12:00:00Z",
            "critical": False,
            "input_data": {},
        }
        envelope.update(payload)
        return envelope

    def register_session(
        self,
        external_id: str,
        machine_id: str = "21000000-0000-4000-8000-000000000002",
        source: str = "Claude Code",
        project_id: str | None = None,
        parent_session_id: str | None = None,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        """Register a new session via /sessions/register endpoint.

        Returns response with 'id' (internal session ID), 'external_id', 'machine_id'.
        """
        payload: dict[str, Any] = {
            "external_id": external_id,
            "machine_id": machine_id,
            "source": source,
        }
        if project_id:
            payload["project_id"] = project_id
        if parent_session_id:
            payload["parent_session_id"] = parent_session_id
        if cwd:
            payload["cwd"] = cwd

        response = self.client.post("/api/sessions/register", json=payload)
        response.raise_for_status()
        return cast(dict[str, Any], response.json())

    def session_start(
        self,
        session_id: str,
        machine_id: str = "21000000-0000-4000-8000-000000000002",
        cli_source: str = "claude",
        session_start_source: str = "startup",
        project_id: str | None = None,
        cwd: str | None = None,
        terminal_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Simulate session start hook event via /hooks/execute endpoint."""
        input_data: dict[str, Any] = {
            "session_id": session_id,
            "machine_id": machine_id,
            "source": session_start_source,
        }
        if project_id:
            input_data["project_id"] = project_id
        if cwd:
            input_data["cwd"] = cwd
        if terminal_context:
            input_data["terminal_context"] = terminal_context

        hook_type = {
            "claude": "session-start",
            "grok": "session_start",
        }.get(cli_source, "SessionStart")
        payload = {
            "critical": True,
            "hook_type": hook_type,
            "source": cli_source,
            "input_data": input_data,
        }

        headers = {"X-Gobby-Project-Id": project_id} if project_id else None
        response = self.client.post(
            "/api/hooks/execute",
            json=self._hook_envelope(**payload),
            headers=headers,
        )
        response.raise_for_status()
        return cast(dict[str, Any], response.json())

    def session_end(
        self,
        session_id: str,
        machine_id: str = "21000000-0000-4000-8000-000000000002",
        source: str = "claude",
    ) -> dict[str, Any]:
        """Simulate session end hook event via /hooks/execute endpoint."""
        payload = {
            "hook_type": "session-end",
            "source": source,
            "input_data": {
                "session_id": session_id,
                "machine_id": machine_id,
            },
        }

        response = self.client.post("/api/hooks/execute", json=self._hook_envelope(**payload))
        response.raise_for_status()
        return cast(dict[str, Any], response.json())

    def tool_use(
        self,
        session_id: str,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
        source: str = "claude",
    ) -> dict[str, Any]:
        """Simulate tool use hook event via /hooks/execute endpoint."""
        payload = {
            "hook_type": "tool-use",
            "source": source,
            "input_data": {
                "session_id": session_id,
                "tool_name": tool_name,
                "tool_input": tool_input or {},
            },
        }

        response = self.client.post("/api/hooks/execute", json=self._hook_envelope(**payload))
        response.raise_for_status()
        return cast(dict[str, Any], response.json())

    def post_tool_use(
        self,
        session_id: str,
        *,
        cli_source: str,
        input_data: dict[str, Any],
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Simulate a provider-native post-tool hook via /hooks/execute."""
        event_data = {
            **input_data,
            "session_id": session_id,
            "machine_id": "21000000-0000-4000-8000-000000000002",
        }
        if project_id:
            event_data["project_id"] = project_id

        hook_type = {
            "claude": "post-tool-use",
            "grok": "post_tool_use",
        }.get(cli_source, "PostToolUse")
        payload = {
            "hook_type": hook_type,
            "source": cli_source,
            "input_data": event_data,
        }
        headers = {"X-Gobby-Project-Id": project_id} if project_id else None
        response = self.client.post(
            "/api/hooks/execute",
            json=self._hook_envelope(**payload),
            headers=headers,
        )
        response.raise_for_status()
        return cast(dict[str, Any], response.json())

    def user_prompt_submit(
        self,
        session_id: str,
        prompt: str,
        source: str = "claude",
        machine_id: str = "21000000-0000-4000-8000-000000000002",
        cwd: str | None = None,
        project_id: str | None = None,
        terminal_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Simulate a provider-specific turn-start/user-prompt hook."""
        hook_type_by_source = {
            "claude": "user-prompt-submit",
            "codex": "UserPromptSubmit",
            "droid": "UserPromptSubmit",
            "grok": "user_prompt_submit",
            "qwen": "UserPromptSubmit",
        }
        hook_type = hook_type_by_source[source]
        input_data: dict[str, Any] = {
            "session_id": session_id,
            "machine_id": machine_id,
        }
        if cwd:
            input_data["cwd"] = cwd
        if project_id:
            input_data["project_id"] = project_id
        if terminal_context:
            input_data["terminal_context"] = terminal_context

        if source in {"claude", "droid"}:
            input_data["user_prompt"] = prompt
        else:
            input_data["prompt"] = prompt
        payload = {
            "hook_type": hook_type,
            "source": source,
            "input_data": input_data,
        }

        response = self.client.post("/api/hooks/execute", json=self._hook_envelope(**payload))
        response.raise_for_status()
        return cast(dict[str, Any], response.json())

    def _grok_active_hook(
        self,
        hook_type: str,
        session_id: str,
        envelope_id: str,
        *,
        project_id: str | None = None,
        cwd: str | None = None,
        **input_fields: Any,
    ) -> dict[str, Any]:
        """Send an envelope-backed Grok hook through the isolated daemon."""
        input_data: dict[str, Any] = {"session_id": session_id, **input_fields}
        if cwd:
            input_data["cwd"] = cwd
        if project_id:
            input_data["project_id"] = project_id

        headers = {"X-Gobby-Envelope-Id": envelope_id}
        if project_id:
            headers["X-Gobby-Project-Id"] = project_id
        payload = {
            "hook_type": hook_type,
            "source": "grok",
            "input_data": input_data,
        }
        response = self.client.post(
            "/api/hooks/execute",
            json=self._hook_envelope(**payload),
            headers=headers,
        )
        response.raise_for_status()
        return cast(dict[str, Any], response.json())

    def grok_pre_tool_use(
        self,
        session_id: str,
        tool_name: str,
        envelope_id: str,
        tool_input: dict[str, Any] | None = None,
        *,
        project_id: str | None = None,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        """Simulate Grok's active PreToolUse delivery channel."""
        return self._grok_active_hook(
            "pre_tool_use",
            session_id,
            envelope_id,
            project_id=project_id,
            cwd=cwd,
            tool_name=tool_name,
            tool_input=tool_input or {},
        )

    def grok_stop(
        self,
        session_id: str,
        envelope_id: str,
        *,
        project_id: str | None = None,
        cwd: str | None = None,
        last_assistant_message: str = "",
    ) -> dict[str, Any]:
        """Simulate Grok's active Stop delivery channel."""
        return self._grok_active_hook(
            "stop",
            session_id,
            envelope_id,
            project_id=project_id,
            cwd=cwd,
            last_assistant_message=last_assistant_message,
            stop_hook_active=True,
        )

    def register_test_agent(
        self,
        run_id: str,
        session_id: str,
        parent_session_id: str,
        mode: str = "interactive",
        agent_name: str | None = None,
        status: str = "running",
    ) -> dict[str, Any]:
        """Register a test agent in the running agent registry.

        This is used for E2E testing of inter-agent messaging without
        actually spawning agent processes.
        """
        payload = {
            "run_id": run_id,
            "session_id": session_id,
            "parent_session_id": parent_session_id,
            "mode": mode,
            "status": status,
        }
        if agent_name is not None:
            payload["agent_name"] = agent_name

        response = self.client.post("/api/admin/test/register-agent", json=payload)
        response.raise_for_status()
        return cast(dict[str, Any], response.json())

    def unregister_test_agent(self, run_id: str) -> dict[str, Any]:
        """Unregister a test agent from the running agent registry."""
        response = self.client.delete(f"/api/admin/test/unregister-agent/{run_id}")
        response.raise_for_status()
        return cast(dict[str, Any], response.json())

    def register_test_project(
        self,
        project_id: str,
        name: str,
        repo_path: str | None = None,
    ) -> dict[str, Any]:
        """Register a test project in the database.

        This ensures the project exists in the projects table so sessions
        can be created with valid project_ids.
        """
        payload = {
            "project_id": project_id,
            "name": name,
        }
        if repo_path:
            payload["repo_path"] = repo_path

        response = self.client.post("/api/admin/test/register-project", json=payload)
        assert response.is_success, response.text
        response.raise_for_status()
        return cast(dict[str, Any], response.json())

    def set_session_usage(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> dict[str, Any]:
        """Set usage statistics for a test session.

        This is for E2E testing of usage reporting and related metrics surfaces.
        """
        payload = {
            "session_id": session_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "cache_read_tokens": cache_read_tokens,
        }

        response = self.client.post("/api/admin/test/set-session-usage", json=payload)
        response.raise_for_status()
        return cast(dict[str, Any], response.json())


@pytest.fixture(scope="function")
def cli_events(daemon_instance: DaemonInstance) -> Generator[CLIEventSimulator]:
    """CLI event simulator for daemon instance."""
    simulator = CLIEventSimulator(
        daemon_instance.http_url,
        daemon_token(daemon_instance.gobby_home),
    )
    yield simulator
    simulator.close()


# --- MCP Client Helpers ---


class MCPTestClient:
    """Helper for testing MCP proxy functionality.

    Set ``session_id`` to have the client send ``X-Gobby-Session-Id`` on tool
    calls. The daemon's MCP execution endpoint populates the SessionContext
    ContextVar from this header, which tools like ``gobby-tasks.create_task``
    now require (session_id was removed from their argument schemas). Set
    ``agent_run_id`` when the session belongs to an active agent run.
    """

    def __init__(self, daemon_url: str, token: str):
        self.daemon_url = daemon_url
        self.client = httpx.Client(
            base_url=daemon_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        self.session_id: str | None = None
        self.agent_run_id: str | None = None

    def close(self) -> None:
        """Close the HTTP client."""
        self.client.close()

    def _session_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.session_id:
            headers["X-Gobby-Session-Id"] = self.session_id
        if self.agent_run_id:
            headers[AGENT_RUN_ID_HEADER] = self.agent_run_id
        return headers

    def list_servers(self) -> list[dict[str, Any]]:
        """List available MCP servers."""
        response = self.client.get("/api/mcp/servers")
        response.raise_for_status()
        payload = cast(dict[str, Any], response.json())
        return cast(list[dict[str, Any]], payload.get("servers", []))

    def list_tools(self, server_name: str | None = None) -> list[dict[str, Any]]:
        """List tools, optionally filtered by server.

        Returns a flat list of tools, each with 'server' key added.
        """
        params = {}
        if server_name:
            # API uses server_filter parameter
            params["server_filter"] = server_name

        response = self.client.get("/api/mcp/tools", params=params)
        response.raise_for_status()
        data = response.json()

        # Handle both dict (by server) and list (flat) formats
        tools_data = data.get("tools", data)

        if isinstance(tools_data, dict):
            # Convert dict format to flat list
            flat_tools = []
            for srv_name, srv_tools in tools_data.items():
                for tool in srv_tools:
                    tool_copy = dict(tool)
                    tool_copy["server"] = srv_name
                    flat_tools.append(tool_copy)
            return flat_tools
        elif isinstance(tools_data, list):
            return tools_data
        else:
            return []

    def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call an MCP tool."""
        payload = {
            "server_name": server_name,
            "tool_name": tool_name,
            "arguments": arguments or {},
        }

        # Endpoint is /mcp/tools/call
        response = self.client.post(
            "/api/mcp/tools/call", json=payload, headers=self._session_headers()
        )
        response.raise_for_status()
        return cast(dict[str, Any], response.json())

    def get_tool_schema(self, server_name: str, tool_name: str) -> dict[str, Any]:
        """Get full schema for a tool."""
        # Endpoint is POST /mcp/tools/schema with JSON body
        response = self.client.post(
            "/api/mcp/tools/schema",
            json={"server_name": server_name, "tool_name": tool_name},
            headers=self._session_headers(),
        )
        response.raise_for_status()
        return cast(dict[str, Any], response.json())


@pytest.fixture(scope="function")
def mcp_client(daemon_instance: DaemonInstance) -> Generator[MCPTestClient]:
    """MCP test client for daemon instance."""
    client = MCPTestClient(
        daemon_instance.http_url,
        daemon_token(daemon_instance.gobby_home),
    )
    yield client
    client.close()


# --- Async MCP Client ---


class AsyncMCPTestClient:
    """Async helper for testing MCP proxy functionality.

    See ``MCPTestClient`` for the session-context rationale.
    """

    def __init__(self, daemon_url: str, token: str):
        self.daemon_url = daemon_url
        self.client = httpx.AsyncClient(
            base_url=daemon_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
        self.session_id: str | None = None

    async def close(self) -> None:
        """Close the HTTP client."""
        await self.client.aclose()

    def _session_headers(self) -> dict[str, str]:
        return {"X-Gobby-Session-Id": self.session_id} if self.session_id else {}

    async def list_servers(self) -> list[dict[str, Any]]:
        """List available MCP servers."""
        response = await self.client.get("/api/mcp/servers")
        response.raise_for_status()
        payload = cast(dict[str, Any], response.json())
        return cast(list[dict[str, Any]], payload.get("servers", []))

    async def list_tools(self, server_name: str | None = None) -> list[dict[str, Any]]:
        """List tools, optionally filtered by server.

        Returns a flat list of tools, each with 'server' key added.
        """
        params = {}
        if server_name:
            # API uses server_filter parameter
            params["server_filter"] = server_name

        response = await self.client.get("/api/mcp/tools", params=params)
        response.raise_for_status()
        data = response.json()

        # Handle both dict (by server) and list (flat) formats
        tools_data = data.get("tools", data)

        if isinstance(tools_data, dict):
            # Convert dict format to flat list
            flat_tools = []
            for srv_name, srv_tools in tools_data.items():
                for tool in srv_tools:
                    tool_copy = dict(tool)
                    tool_copy["server"] = srv_name
                    flat_tools.append(tool_copy)
            return flat_tools
        elif isinstance(tools_data, list):
            return tools_data
        else:
            return []

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call an MCP tool."""
        payload = {
            "server_name": server_name,
            "tool_name": tool_name,
            "arguments": arguments or {},
        }

        # Endpoint is /mcp/tools/call
        response = await self.client.post(
            "/api/mcp/tools/call", json=payload, headers=self._session_headers()
        )
        response.raise_for_status()
        return cast(dict[str, Any], response.json())


@pytest_asyncio.fixture
async def async_mcp_client(
    daemon_instance: DaemonInstance,
) -> AsyncGenerator[AsyncMCPTestClient]:
    """Async MCP test client for daemon instance."""
    client = AsyncMCPTestClient(
        daemon_instance.http_url,
        daemon_token(daemon_instance.gobby_home),
    )
    yield client
    await client.close()


# --- Production Leak Detection ---


_SNAPSHOT_EXCLUDED_DIRS = {"skill-cache"}

# Directories recorded one level deep and never descended into. Task worktrees
# are full source checkouts whose file count is unbounded and unrelated to what
# this guard protects: ~/.gobby/worktrees has held 840k files / 85 GiB, which
# cost ~36s per snapshot and ~72s per test. Recording each worktree's top-level
# entry still catches a test creating one; descending only prices in churn from
# concurrent agents.
_SHALLOW_SNAPSHOT_PREFIXES = ("worktrees/",)


def _is_shallow_snapshot_child(rel_dir: str) -> bool:
    """Return true for a directory nested inside a shallow-snapshot root."""
    return any(
        rel_dir.startswith(prefix) and rel_dir != prefix for prefix in _SHALLOW_SNAPSHOT_PREFIXES
    )


def _snapshot_dir(path: Path) -> dict[str, float]:
    """Return {relative_path: mtime} for entries under *path*.

    Uses os.scandir so each entry's stat comes from the directory read itself
    rather than a separate syscall per file. Symlinks are recorded as entries
    rather than followed, so a symlink appearing in ~/.gobby is caught.
    """
    if not path.exists():
        return {}

    snapshot: dict[str, float] = {}
    stack: list[tuple[str, str]] = [(str(path), "")]
    while stack:
        current, rel_root = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            rel_path = f"{rel_root}{entry.name}"
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in _SNAPSHOT_EXCLUDED_DIRS:
                        continue
                    rel_dir = f"{rel_path}/"
                    if rel_dir.startswith(_ALWAYS_EXEMPT_PREFIXES):
                        continue
                    if _is_shallow_snapshot_child(rel_dir):
                        snapshot[rel_path] = entry.stat(follow_symlinks=False).st_mtime
                        continue
                    stack.append((entry.path, rel_dir))
                    continue
                snapshot[rel_path] = entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
    return snapshot


def _production_daemon_running() -> bool:
    """Check if a production gobby daemon is listening on port 60887.

    Uses retry logic to handle transient unresponsiveness, plus a PID file
    fallback for when the daemon is alive but briefly unresponsive to TCP.
    """
    # Try TCP probe with retries (daemon may be briefly busy)
    for _ in range(3):
        try:
            with socket.create_connection(("localhost", 60887), timeout=0.3):
                return True
        except (ConnectionRefusedError, OSError, TimeoutError):
            time.sleep(0.3)

    # Fallback: check for PID file with live process
    pid_file = Path.home() / ".gobby" / "gobby.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # Check if process is alive (signal 0)
            return True
        except (ValueError, OSError, ProcessLookupError):
            pass

    return False


# Known daemon artifacts that the production daemon may create/touch
_DAEMON_ARTIFACTS = {"gobby.pid", "ui.pid", "shutdown_intent_active.json"}
_PRODUCTION_DAEMON_ARTIFACT_PREFIXES = (
    "cache/transcript-indexes/",
    "grants/",
    "logs/",
    "runtime/managed-executions/",
    "session_summaries/",
    "session_transcripts/",
    "worktrees/",
)

# Transient per-daemon-instance files that we never flag as a leak. The test
# daemon runs with HOME overridden to a tmp dir, so any write to real ~/.gobby/
# for one of these paths is necessarily the production daemon on the dev box
# (or a subsequent one). Their creation is inherently racy against the
# before/after snapshot — if the production daemon writes during the test
# window, the prod-running TCP/PID-file detector may not catch it in time.
# A real sandbox escape would also leak db/config files that aren't listed
# here, so these omissions do not weaken the check.
_ALWAYS_EXEMPT_BASENAMES = {"shutdown_intent_active.json"}
_ALWAYS_EXEMPT_PREFIXES = ("hooks/inbox/", "session_wiki/")


def _is_production_daemon_artifact(rel_path: str) -> bool:
    """Return whether a running canonical daemon may create this path."""
    basename = Path(rel_path).name
    return (
        basename in _DAEMON_ARTIFACTS
        or basename.endswith((".pid", "-journal", "-shm", "-wal"))
        or rel_path.startswith(_PRODUCTION_DAEMON_ARTIFACT_PREFIXES)
    )


@pytest.fixture(autouse=True)
def assert_no_external_writes() -> Generator[None]:
    """Fail the test if the E2E daemon created new files in real ~/.gobby/.

    This catches regressions where a new feature uses an un-overridden
    default path (database, qdrant, session_summaries, MCP config, etc.)
    that resolves to the user's real home directory instead of the test
    temp dir.

    When a production daemon is running concurrently, existing files
    (database, logs) will be modified by that daemon — so we only flag
    *newly created* files. In CI where no production daemon runs,
    we flag both creations and modifications.

    Known daemon artifacts (PID files, log files) are ignored when a
    production daemon is detected, since these are created by the
    daemon's lifecycle, not by the test.
    """
    real_gobby = Path.home() / ".gobby"
    prod_before = _production_daemon_running()
    before = _snapshot_dir(real_gobby)

    yield

    after = _snapshot_dir(real_gobby)
    prod_after = _production_daemon_running()

    # If production daemon was running at any point, it's the likely source
    prod_running = prod_before or prod_after

    leaked: list[str] = []
    for rel_path, mtime in after.items():
        if rel_path not in before:
            # CREATED file — check if it's a known daemon artifact
            basename = Path(rel_path).name
            if rel_path.startswith(_ALWAYS_EXEMPT_PREFIXES):
                continue
            if basename in _ALWAYS_EXEMPT_BASENAMES:
                continue  # Transient per-daemon file — see _ALWAYS_EXEMPT_BASENAMES
            if prod_running and _is_production_daemon_artifact(rel_path):
                continue  # Known production daemon artifact
            leaked.append(f"  CREATED: ~/.gobby/{rel_path}")
        elif mtime != before[rel_path] and not prod_running and not rel_path.startswith("logs/"):
            # Only flag modifications when no production daemon is running,
            # since a running daemon continuously writes to its db and logs.
            # Log file mtime changes are always the production daemon (test
            # daemon writes to its temp dir), so exempt them unconditionally.
            # PostgreSQL WAL/SHM files can be touched by any process that opens
            # the database (even read-only), so exempt them as well.
            basename = Path(rel_path).name
            if rel_path.startswith("worktrees/"):
                # Shallow-snapshotted: a worktree directory's mtime changes
                # whenever a concurrent agent writes into it.
                continue
            if basename.endswith(("-shm", "-wal", "-journal")):
                continue
            leaked.append(f"  MODIFIED: ~/.gobby/{rel_path}")

    if leaked:
        pytest.fail(
            "E2E test wrote to real ~/.gobby/ — the daemon escaped its sandbox!\n"
            + "\n".join(leaked)
        )


# --- Process Cleanup ---


def _cleanup_orphan_gobby_processes() -> None:
    """Clean up any orphan gobby processes from previous e2e test runs.

    IMPORTANT: Only kills processes that are clearly from e2e tests
    (identified by gobby_e2e_ temp directory in cmdline), NOT the user's
    actual running daemon.
    """
    import psutil

    current_pid = os.getpid()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if proc.pid == current_pid:
                continue

            cmdline = " ".join(proc.cmdline())
            # Only kill if it's a gobby runner AND has e2e test markers in path
            if "gobby.runner" in cmdline and "gobby_e2e_" in cmdline:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except psutil.TimeoutExpired:
                    proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


@pytest.fixture(scope="session", autouse=True)
def cleanup_orphan_processes() -> Generator[None]:
    """Clean up any orphan gobby e2e test processes after test session."""
    yield

    # Post-session cleanup only (don't kill user's daemon on startup)
    _cleanup_orphan_gobby_processes()


# --- Utility Fixtures ---


@pytest.fixture
def wait_for_condition() -> Callable[[Callable[[], bool], float, float, str], bool]:
    """Fixture providing a polling utility for async conditions."""

    def _wait(
        condition_fn: Callable[[], bool],
        timeout: float = 5.0,
        poll_interval: float = 0.1,
        description: str = "condition",
    ) -> bool:
        """Wait for a condition function to return True."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                if condition_fn():
                    return True
            except Exception:
                pass
            time.sleep(poll_interval)
        return False

    return _wait
