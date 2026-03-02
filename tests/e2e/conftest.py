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
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

# Mark all tests in this directory as e2e tests
pytestmark = pytest.mark.e2e


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

    @property
    def http_url(self) -> str:
        """HTTP base URL."""
        return f"http://localhost:{self.http_port}"

    @property
    def ws_url(self) -> str:
        """WebSocket URL."""
        return f"ws://localhost:{self.ws_port}"

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


def prepare_daemon_env(
    base_env: dict[str, str] | None = None,
    *,
    home_dir: str | Path | None = None,
) -> dict[str, str]:
    """Prepare environment variables for spawning a daemon subprocess.

    This handles the critical setup that's easy to miss when manually spawning daemons:
    1. Sets PYTHONPATH to include the src directory
    2. Removes GOBBY_DATABASE_PATH so daemon uses its config file's database_path
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

    # Remove test-process env vars so daemon uses its own isolated config/DB
    # (protect_production_resources sets these for the test process, but we don't
    # want the daemon subprocess to inherit them)
    env.pop("GOBBY_DATABASE_PATH", None)
    env.pop("GOBBY_TEST_PROTECT", None)
    env.pop("GOBBY_CONFIG_FILE", None)

    # Disable any LLM providers to avoid external calls
    env["ANTHROPIC_API_KEY"] = ""
    env["OPENAI_API_KEY"] = ""
    env["GEMINI_API_KEY"] = ""

    # Override HOME so that ~/.gobby resolves to <temp>/.gobby instead of
    # the user's real home directory. This is the single most effective
    # isolation measure: it catches every expanduser() call in the daemon.
    if home_dir is not None:
        env["HOME"] = str(home_dir)

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
            port = s.getsockname()[1]

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
    """Wait for daemon to respond to health check."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            response = httpx.get(f"http://localhost:{port}/api/admin/status", timeout=2.0)
            if response.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadTimeout):
            time.sleep(0.5)
    return False


def terminate_process_tree(pid: int, timeout: float = 5.0) -> None:
    """Terminate a process and all its children."""
    import psutil

    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)

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
    """Create an isolated project directory for E2E tests."""
    with tempfile.TemporaryDirectory(prefix="gobby_e2e_") as tmpdir:
        project_dir = Path(tmpdir)
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
                    "id": "e2e-test-project",
                    "name": "E2E Test Project",
                    "repo_path": str(project_dir),
                }
            )
        )

        # Copy shared workflows and agents for spawn_agent tests
        shared_dir = Path(__file__).parent.parent.parent / "src" / "gobby" / "install" / "shared"
        if shared_dir.exists():
            import shutil

            # Copy workflows
            shared_workflows = shared_dir / "workflows"
            if shared_workflows.exists():
                target_workflows = gobby_dir / "workflows"
                target_workflows.mkdir(parents=True, exist_ok=True)
                for wf_file in shared_workflows.glob("*.yaml"):
                    shutil.copy2(wf_file, target_workflows / wf_file.name)

            # Copy agents
            shared_agents = shared_dir / "agents"
            if shared_agents.exists():
                target_agents = gobby_dir / "agents"
                target_agents.mkdir(parents=True, exist_ok=True)
                for agent_file in shared_agents.glob("*.yaml"):
                    shutil.copy2(agent_file, target_agents / agent_file.name)

        yield project_dir


@pytest.fixture(scope="function")
def e2e_config(e2e_project_dir: Path) -> Generator[tuple[Path, int, int]]:
    """Create an isolated config file with unique ports."""
    http_port = find_free_port()
    ws_port = find_free_port()

    gobby_home = e2e_project_dir / ".gobby-home"
    gobby_home.mkdir(parents=True, exist_ok=True)

    config_path = gobby_home / "config.yaml"
    db_path = gobby_home / "gobby-hub.db"
    log_dir = gobby_home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    config_content = f"""
daemon_port: {http_port}
test_mode: true
database_path: "{db_path}"

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

memory:
  qdrant_path: "{gobby_home}/services/qdrant"

session_summary:
  summary_file_path: "{gobby_home}/session_summaries"

watchdog:
  enabled: false

conductor:
  daily_budget_usd: 1.0
  warning_threshold: 0.8
  throttle_threshold: 0.9
  tracking_window_days: 7

metrics:
  daily_budget_usd: 1.0
"""

    config_path.write_text(config_content)

    # Write bootstrap.yaml so the runner picks up ports and db_path
    # (load_config Phase 1 reads bootstrap.yaml, not config.yaml)
    bootstrap_path = gobby_home / "bootstrap.yaml"
    bootstrap_content = f"""
daemon_port: {http_port}
database_path: "{db_path}"
bind_host: localhost
websocket_port: {ws_port}
"""
    bootstrap_path.write_text(bootstrap_content)

    yield config_path, http_port, ws_port


@pytest.fixture(scope="function")
def daemon_instance(
    e2e_project_dir: Path,
    e2e_config: tuple[Path, int, int],
) -> Generator[DaemonInstance]:
    """
    Spawn an isolated daemon instance for E2E testing.

    Yields a DaemonInstance with running daemon, then cleans up on teardown.
    """
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

    # Start daemon process
    with open(log_file, "w") as log_f, open(error_log_file, "w") as err_f:
        process = subprocess.Popen(
            [sys.executable, "-m", "gobby.runner", "--config", str(config_path)],
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
        db_path=gobby_home / "gobby-hub.db",
        config_path=config_path,
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
    with httpx.Client(base_url=daemon_instance.http_url, timeout=10.0) as client:
        yield client


@pytest_asyncio.fixture
async def async_daemon_client(
    daemon_instance: DaemonInstance,
) -> AsyncGenerator[httpx.AsyncClient]:
    """Async HTTP client configured for daemon instance."""
    async with httpx.AsyncClient(base_url=daemon_instance.http_url, timeout=10.0) as client:
        yield client


# --- CLI Event Helpers ---


class CLIEventSimulator:
    """Helper for simulating CLI hook events and session registration."""

    def __init__(self, daemon_url: str):
        self.daemon_url = daemon_url
        self.client = httpx.Client(base_url=daemon_url, timeout=10.0)

    def close(self) -> None:
        """Close the HTTP client."""
        self.client.close()

    def register_session(
        self,
        external_id: str,
        machine_id: str = "test-machine",
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
        return response.json()

    def session_start(
        self,
        session_id: str,
        machine_id: str = "test-machine",
        source: str = "claude",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Simulate session start hook event via /hooks/execute endpoint."""
        input_data = {
            "session_id": session_id,
            "machine_id": machine_id,
        }
        if project_id:
            input_data["project_id"] = project_id

        payload = {
            "hook_type": "session-start",
            "source": source,
            "input_data": input_data,
        }

        response = self.client.post("/api/hooks/execute", json=payload)
        response.raise_for_status()
        return response.json()

    def session_end(
        self,
        session_id: str,
        machine_id: str = "test-machine",
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

        response = self.client.post("/api/hooks/execute", json=payload)
        response.raise_for_status()
        return response.json()

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

        response = self.client.post("/api/hooks/execute", json=payload)
        response.raise_for_status()
        return response.json()

    def register_test_agent(
        self,
        run_id: str,
        session_id: str,
        parent_session_id: str,
        mode: str = "terminal",
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
        }

        response = self.client.post("/api/admin/test/register-agent", json=payload)
        response.raise_for_status()
        return response.json()

    def unregister_test_agent(self, run_id: str) -> dict[str, Any]:
        """Unregister a test agent from the running agent registry."""
        response = self.client.delete(f"/api/admin/test/unregister-agent/{run_id}")
        response.raise_for_status()
        return response.json()

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
        response.raise_for_status()
        return response.json()

    def set_session_usage(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
        total_cost_usd: float = 0.0,
    ) -> dict[str, Any]:
        """Set usage statistics for a test session.

        This is for E2E testing of token budget throttling.
        """
        payload = {
            "session_id": session_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "cache_read_tokens": cache_read_tokens,
            "total_cost_usd": total_cost_usd,
        }

        response = self.client.post("/api/admin/test/set-session-usage", json=payload)
        response.raise_for_status()
        return response.json()


@pytest.fixture(scope="function")
def cli_events(daemon_instance: DaemonInstance) -> Generator[CLIEventSimulator]:
    """CLI event simulator for daemon instance."""
    simulator = CLIEventSimulator(daemon_instance.http_url)
    yield simulator
    simulator.close()


# --- MCP Client Helpers ---


class MCPTestClient:
    """Helper for testing MCP proxy functionality."""

    def __init__(self, daemon_url: str):
        self.daemon_url = daemon_url
        self.client = httpx.Client(base_url=daemon_url, timeout=30.0)

    def close(self) -> None:
        """Close the HTTP client."""
        self.client.close()

    def list_servers(self) -> list[dict[str, Any]]:
        """List available MCP servers."""
        response = self.client.get("/api/mcp/servers")
        response.raise_for_status()
        return response.json().get("servers", [])

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
        response = self.client.post("/api/mcp/tools/call", json=payload)
        response.raise_for_status()
        return response.json()

    def get_tool_schema(self, server_name: str, tool_name: str) -> dict[str, Any]:
        """Get full schema for a tool."""
        # Endpoint is POST /mcp/tools/schema with JSON body
        response = self.client.post(
            "/api/mcp/tools/schema",
            json={"server_name": server_name, "tool_name": tool_name},
        )
        response.raise_for_status()
        return response.json()


@pytest.fixture(scope="function")
def mcp_client(daemon_instance: DaemonInstance) -> Generator[MCPTestClient]:
    """MCP test client for daemon instance."""
    client = MCPTestClient(daemon_instance.http_url)
    yield client
    client.close()


# --- Async MCP Client ---


class AsyncMCPTestClient:
    """Async helper for testing MCP proxy functionality."""

    def __init__(self, daemon_url: str):
        self.daemon_url = daemon_url
        self.client = httpx.AsyncClient(base_url=daemon_url, timeout=30.0)

    async def close(self) -> None:
        """Close the HTTP client."""
        await self.client.aclose()

    async def list_servers(self) -> list[dict[str, Any]]:
        """List available MCP servers."""
        response = await self.client.get("/api/mcp/servers")
        response.raise_for_status()
        return response.json().get("servers", [])

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
        response = await self.client.post("/api/mcp/tools/call", json=payload)
        response.raise_for_status()
        return response.json()


@pytest_asyncio.fixture
async def async_mcp_client(
    daemon_instance: DaemonInstance,
) -> AsyncGenerator[AsyncMCPTestClient]:
    """Async MCP test client for daemon instance."""
    client = AsyncMCPTestClient(daemon_instance.http_url)
    yield client
    await client.close()


# --- Production Leak Detection ---


def _snapshot_dir(path: Path) -> dict[str, float]:
    """Return {relative_path: mtime} for all files under *path*."""
    if not path.exists():
        return {}
    return {
        str(p.relative_to(path)): p.stat().st_mtime
        for p in path.rglob("*")
        if p.is_file()
    }


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
_DAEMON_ARTIFACTS = {"gobby.pid", "ui.pid", "watchdog.pid"}


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
    daemon's watchdog cycle, not by the test.
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
            if prod_running and (
                basename in _DAEMON_ARTIFACTS
                or rel_path.startswith("logs/")
                or basename.endswith(".pid")
            ):
                continue  # Known production daemon artifact
            leaked.append(f"  CREATED: ~/.gobby/{rel_path}")
        elif mtime != before[rel_path] and not prod_running and not rel_path.startswith("logs/"):
            # Only flag modifications when no production daemon is running,
            # since a running daemon continuously writes to its db and logs.
            # Log file mtime changes are always the production daemon (test
            # daemon writes to its temp dir), so exempt them unconditionally.
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
def wait_for_condition():
    """Fixture providing a polling utility for async conditions."""

    def _wait(
        condition_fn,
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
