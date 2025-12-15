"""
Gobby CLI commands.

Command-line interface for managing the Gobby daemon using Click framework.
Local-first version: no platform authentication required.
"""

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import click
from gobby.config.app import load_config

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool = False) -> None:
    """
    Configure logging for CLI.

    Args:
        verbose: If True, enable DEBUG level logging
    """
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Silence noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _is_claude_code_installed() -> bool:
    """Check if Claude Code CLI is installed.

    Claude Code is installed via npm: npm install -g @anthropic-ai/claude-code
    The binary is named 'claude'.

    Returns:
        True if Claude Code CLI is found in PATH
    """
    import shutil

    return shutil.which("claude") is not None


def _is_gemini_cli_installed() -> bool:
    """Check if Gemini CLI is installed.

    Gemini CLI is installed via npm: npm install -g @google/gemini-cli
    The binary is named 'gemini'.

    Returns:
        True if Gemini CLI is found in PATH
    """
    import shutil

    return shutil.which("gemini") is not None


def _is_codex_cli_installed() -> bool:
    """Check if OpenAI Codex CLI is installed.

    Codex CLI is installed via npm: npm install -g @openai/codex
    Or via Homebrew: brew install codex
    The binary is named 'codex'.

    Returns:
        True if Codex CLI is found in PATH
    """
    import shutil

    return shutil.which("codex") is not None


def format_uptime(seconds: float) -> str:
    """
    Format uptime in human-readable format.

    Args:
        seconds: Uptime in seconds

    Returns:
        Formatted string like "1h 23m 45s"
    """
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 or not parts:
        parts.append(f"{secs}s")

    return " ".join(parts)


def is_port_available(port: int, host: str = "localhost") -> bool:
    """
    Check if a port is available for binding.

    Args:
        port: Port number to check
        host: Host address to bind to

    Returns:
        True if port is available, False otherwise
    """
    import socket

    # Try to bind to the port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        sock.bind((host, port))
        sock.close()
        return True
    except OSError:
        sock.close()
        return False


def wait_for_port_available(port: int, host: str = "localhost", timeout: float = 5.0) -> bool:
    """
    Wait for a port to become available.

    Args:
        port: Port number to check
        host: Host address to bind to
        timeout: Maximum time to wait in seconds

    Returns:
        True if port became available, False if timeout
    """
    start_time = time.time()

    while (time.time() - start_time) < timeout:
        if is_port_available(port, host):
            return True
        time.sleep(0.1)

    return False


def kill_all_gobby_daemons() -> int:
    """
    Find and kill all gobby DAEMON processes (not CLI commands).

    Only kills processes that are actually running daemon servers,
    not CLI invocations or other tools.

    Detection methods:
    1. Matches gobby.runner (the main daemon process)
    2. Matches processes listening on daemon ports (8765/8766)

    Returns:
        Number of processes killed
    """
    import psutil

    # Load config to get the configured ports
    try:
        config = load_config(create_default=False)
        http_port = config.daemon_port
        ws_port = config.websocket.port
    except Exception:
        # Fallback to defaults if config can't be loaded
        http_port = 8765
        ws_port = 8766

    killed_count = 0
    current_pid = os.getpid()
    parent_pid = os.getppid()

    # Get our parent process tree to avoid killing it
    parent_pids = {current_pid, parent_pid}
    try:
        parent_proc = psutil.Process(parent_pid)
        while parent_proc.parent() is not None:
            parent_proc = parent_proc.parent()
            parent_pids.add(parent_proc.pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    # Find all gobby daemon processes
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            # Skip our own process and parent tree
            if proc.pid in parent_pids:
                continue

            # Check if this is a gobby daemon process
            cmdline = proc.cmdline()
            cmdline_str = " ".join(cmdline)

            # Match gobby.runner which is the actual daemon process
            # Started via: python -m gobby.runner
            is_gobby_daemon = (
                "python" in cmdline_str.lower()
                and (
                    # Match gobby.runner (new package)
                    "gobby.runner" in cmdline_str
                    # Also match legacy gobby_client.runner if it exists
                    or "gobby_client.runner" in cmdline_str
                )
                # Exclude CLI invocations
                and "gobby.cli" not in cmdline_str
                and "gobby_client.cli" not in cmdline_str
            )

            # Also check for processes that might be old daemon instances
            # by checking if they're listening on our ports
            if not is_gobby_daemon:
                try:
                    # Check if process has connections on daemon ports
                    connections = proc.connections()
                    for conn in connections:
                        if hasattr(conn, "laddr") and conn.laddr:
                            if conn.laddr.port in [http_port, ws_port]:
                                # Only consider it a daemon if it's a Python process
                                # to avoid killing unrelated services
                                if "python" in cmdline_str.lower():
                                    is_gobby_daemon = True
                                    break
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    pass

            if is_gobby_daemon:
                click.echo(f"Found gobby daemon (PID {proc.pid}): {cmdline_str[:100]}")

                # Try graceful shutdown first (SIGTERM)
                try:
                    proc.send_signal(signal.SIGTERM)
                    # Wait up to 5 seconds for graceful shutdown
                    proc.wait(timeout=5)
                    click.echo(f"Gracefully stopped PID {proc.pid}")
                    killed_count += 1
                except psutil.TimeoutExpired:
                    # Force kill if graceful shutdown fails
                    click.echo(f"Process {proc.pid} didn't stop gracefully, force killing...")
                    proc.kill()
                    proc.wait(timeout=2)
                    click.echo(f"Force killed PID {proc.pid}")
                    killed_count += 1

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            # Process already gone or we can't access it
            pass
        except Exception as e:
            click.echo(f"Warning: Error checking process {proc.pid}: {e}", err=True)

    return killed_count


def _init_local_storage() -> None:
    """Initialize local SQLite storage and run migrations."""
    from gobby.storage.database import LocalDatabase
    from gobby.storage.migrations import run_migrations

    # Create database and run migrations
    db = LocalDatabase()
    run_migrations(db)


@click.group()
@click.option(
    "--config",
    type=click.Path(exists=True),
    help="Path to custom configuration file",
)
@click.pass_context
def cli(ctx: click.Context, config: str | None) -> None:
    """Gobby - Local-first daemon for AI coding assistants."""
    # Store config in context for subcommands
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config(config)


@cli.command()
@click.option(
    "--verbose",
    is_flag=True,
    help="Enable verbose debug output",
)
@click.pass_context
def start(ctx: click.Context, verbose: bool) -> None:
    """Start the Gobby daemon."""
    import subprocess
    import sys
    import time
    from pathlib import Path

    # Get config object
    config = ctx.obj["config"]

    # Get paths from config
    gobby_dir = Path.home() / ".gobby"
    pid_file = gobby_dir / "gobby.pid"
    log_file = Path(config.logging.client).expanduser()
    error_log_file = Path(config.logging.client_error).expanduser()

    gobby_dir.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    error_log_file.parent.mkdir(parents=True, exist_ok=True)

    # Initialize local storage before starting daemon
    click.echo("Initializing local storage...")
    _init_local_storage()

    # Check if already running
    if pid_file.exists():
        try:
            with open(pid_file) as f:
                pid = int(f.read().strip())

            # Check if process is actually running
            try:
                os.kill(pid, 0)
                click.echo(f"Gobby daemon is already running (PID: {pid})", err=True)
                sys.exit(1)
            except ProcessLookupError:
                # Stale PID file
                click.echo(f"Removing stale PID file (PID: {pid})")
                pid_file.unlink()
        except Exception:
            pid_file.unlink()

    # Kill any existing gobby processes
    click.echo("Checking for existing gobby processes...")
    killed_count = kill_all_gobby_daemons()
    if killed_count > 0:
        click.echo(f"Stopped {killed_count} existing process(es)")
        time.sleep(2.0)  # Wait for ports to be released
    else:
        click.echo("No existing processes found")

    # Check ports
    http_port = config.daemon_port
    ws_port = config.websocket.port

    if not is_port_available(http_port):
        click.echo(f"Waiting for HTTP port {http_port} to become available...", err=True)
        if not wait_for_port_available(http_port, timeout=5.0):
            click.echo(f"Error: Port {http_port} is still in use", err=True)
            sys.exit(1)

    if not is_port_available(ws_port):
        click.echo(f"Waiting for WebSocket port {ws_port} to become available...", err=True)
        if not wait_for_port_available(ws_port, timeout=5.0):
            click.echo(f"Error: Port {ws_port} is still in use", err=True)
            sys.exit(1)

    click.echo(f"Ports available (HTTP: {http_port}, WebSocket: {ws_port})")

    # Start the runner as a subprocess
    click.echo("Starting Gobby daemon...")

    # Build command
    cmd = [sys.executable, "-m", "gobby.runner"]
    if verbose:
        cmd.append("--verbose")

    # Open log files
    log_f = open(log_file, "a")
    error_log_f = open(error_log_file, "a")

    try:
        # Start detached subprocess
        process = subprocess.Popen(
            cmd,
            stdout=log_f,
            stderr=error_log_f,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # Detach from terminal
            env=os.environ.copy(),  # Inherit parent's environment (including PATH)
        )

        # Write PID file
        with open(pid_file, "w") as f:
            f.write(str(process.pid))

        # Give it a moment to start
        time.sleep(1.0)

        # Check if still running
        if process.poll() is not None:
            click.echo("Process exited immediately", err=True)
            click.echo(f"  Check logs: {error_log_file}", err=True)
            sys.exit(1)

        # Give server time to fully start
        time.sleep(2.0)

        # Display formatted status
        # Try to verify daemon is responding
        import httpx
        from gobby.utils.status import format_status_message

        daemon_healthy = False
        start_time = time.time()
        max_wait = 15.0

        while (time.time() - start_time) < max_wait:
            try:
                response = httpx.get(f"http://localhost:{http_port}/admin/status", timeout=1.0)
                if response.status_code == 200:
                    daemon_healthy = True
                    break
            except (httpx.ConnectError, httpx.TimeoutException):
                time.sleep(0.5)
                continue

        # Format and display status
        message = format_status_message(
            running=daemon_healthy,
            pid=process.pid,
            pid_file=str(pid_file),
            log_files=str(log_file.parent),
            http_port=http_port,
            websocket_port=ws_port,
        )
        click.echo("")
        click.echo(message)
        click.echo("")

        if not daemon_healthy:
            click.echo("Warning: Daemon started but health check failed")
            click.echo(f"  Check logs: {error_log_file}")

    except Exception as e:
        click.echo(f"Error starting daemon: {e}", err=True)
        sys.exit(1)
    finally:
        log_f.close()
        error_log_f.close()


def _stop_daemon(quiet: bool = False) -> bool:
    """Stop the daemon process. Returns True on success, False on failure.

    Args:
        quiet: If True, suppress output messages

    Returns:
        True if daemon was stopped successfully or wasn't running, False on error
    """
    from pathlib import Path

    pid_file = Path.home() / ".gobby" / "gobby.pid"

    # Read PID from file
    if not pid_file.exists():
        if not quiet:
            click.echo("Gobby daemon is not running (no PID file found)")
        return True

    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
    except Exception as e:
        if not quiet:
            click.echo(f"Error reading PID file: {e}", err=True)
        pid_file.unlink()
        return False

    # Check if process is actually running
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        if not quiet:
            click.echo(f"Gobby daemon is not running (stale PID file with PID {pid})")
        pid_file.unlink()
        return True

    try:
        # Send SIGTERM signal for graceful shutdown
        os.kill(pid, signal.SIGTERM)
        if not quiet:
            click.echo(f"Sent shutdown signal to Gobby daemon (PID {pid})")

        # Wait for shutdown
        max_wait = 5
        for _ in range(max_wait * 10):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                if not quiet:
                    click.echo("Gobby daemon stopped successfully")
                pid_file.unlink()
                return True

        if not quiet:
            click.echo(f"Warning: Process still running after {max_wait}s", err=True)
        return False

    except PermissionError:
        if not quiet:
            click.echo(f"Error: Permission denied to stop process (PID {pid})", err=True)
        return False

    except Exception as e:
        if not quiet:
            click.echo(f"Error stopping daemon: {e}", err=True)
        return False


@cli.command()
@click.pass_context
def stop(ctx: click.Context) -> None:
    """Stop the Gobby daemon."""
    success = _stop_daemon(quiet=False)
    sys.exit(0 if success else 1)


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show Gobby daemon status and information."""
    from pathlib import Path

    import psutil
    from gobby.utils.status import format_status_message

    config = ctx.obj["config"]
    pid_file = Path.home() / ".gobby" / "gobby.pid"
    log_dir = Path(config.logging.client).expanduser().parent

    # Read PID from file
    if not pid_file.exists():
        message = format_status_message(running=False)
        click.echo(message)
        sys.exit(0)

    try:
        with open(pid_file) as f:
            pid = int(f.read().strip())
    except Exception:
        message = format_status_message(running=False)
        click.echo(message)
        sys.exit(0)

    # Check if process is actually running
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        message = format_status_message(running=False)
        click.echo(message)
        click.echo(f"Note: Stale PID file found (PID {pid})")
        sys.exit(0)

    # Get process info
    try:
        process = psutil.Process(pid)
        uptime_seconds = time.time() - process.create_time()
        uptime_str = format_uptime(uptime_seconds)
    except Exception:
        uptime_str = None

    http_port = config.daemon_port
    websocket_port = config.websocket.port

    # Format and display status
    message = format_status_message(
        running=True,
        pid=pid,
        pid_file=str(pid_file),
        log_files=str(log_dir),
        uptime=uptime_str,
        http_port=http_port,
        websocket_port=websocket_port,
    )
    click.echo(message)
    sys.exit(0)


@cli.command()
@click.pass_context
def mcp_server(ctx: click.Context) -> None:
    """
    Run stdio MCP server for Claude Code integration.

    This command starts a stdio-based MCP server that:
    - Auto-starts the daemon if not running
    - Provides daemon lifecycle tools (start/stop/restart)
    - Proxies all HTTP MCP tools from the daemon

    Usage with Claude Code:
      claude mcp add --transport stdio gobby-daemon -- gobby mcp-server
    """
    import asyncio

    from gobby.mcp_proxy.stdio import main as mcp_main

    # Run the stdio MCP server
    asyncio.run(mcp_main())


@cli.command()
@click.option(
    "--verbose",
    is_flag=True,
    help="Enable verbose debug output",
)
@click.pass_context
def restart(ctx: click.Context, verbose: bool) -> None:
    """Restart the Gobby daemon (stop then start)."""
    setup_logging(verbose)

    click.echo("Restarting Gobby daemon...")

    # Stop daemon using helper function (doesn't call sys.exit)
    if not _stop_daemon(quiet=False):
        click.echo("Failed to stop daemon, aborting restart", err=True)
        sys.exit(1)

    # Wait a moment for cleanup
    time.sleep(1)

    # Call start command
    ctx.invoke(start, verbose=verbose)


@cli.command()
@click.option("--name", help="Project name")
@click.option("--github-url", help="GitHub repository URL")
@click.pass_context
def init(ctx: click.Context, name: str | None, github_url: str | None) -> None:
    """Initialize a new Gobby project in the current directory."""
    from pathlib import Path

    from gobby.utils.project_init import initialize_project

    cwd = Path.cwd()

    try:
        result = initialize_project(cwd=cwd, name=name, github_url=github_url)
    except Exception as e:
        click.echo(f"Failed to initialize project: {e}", err=True)
        sys.exit(1)

    if result.already_existed:
        click.echo(f"Project already initialized: {result.project_name}")
        click.echo(f"  Project ID: {result.project_id}")
    else:
        click.echo(f"Initialized project '{result.project_name}' in {cwd}")
        click.echo(f"  Project ID: {result.project_id}")
        click.echo(f"  Config: {cwd / '.gobby' / 'project.json'}")


def _get_install_dir() -> Path:
    """Get the gobby install directory.

    Checks for source directory (development mode) first,
    falls back to package directory.

    Returns:
        Path to the install directory
    """
    import gobby

    package_install_dir = Path(gobby.__file__).parent / "install"

    # Try to find source directory (project root)
    current = Path(gobby.__file__).resolve()
    source_install_dir = None

    for parent in current.parents:
        potential_source = parent / "src" / "gobby" / "install"
        if potential_source.exists():
            source_install_dir = potential_source
            break

    if source_install_dir and source_install_dir.exists():
        return source_install_dir
    return package_install_dir


def _install_claude_hooks(project_path: Path) -> dict[str, Any]:
    """Install Claude Code hooks to a project.

    Args:
        project_path: Path to the project root

    Returns:
        Dict with installation results:
        - success: bool
        - hooks_installed: list of hook names
        - skills_installed: list of skill names
        - error: str (if success=False)
    """
    from shutil import copy2, copytree

    hooks_installed: list[str] = []
    skills_installed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "hooks_installed": hooks_installed,
        "skills_installed": skills_installed,
        "error": None,
    }

    claude_path = project_path / ".claude"
    settings_file = claude_path / "settings.json"

    # Ensure .claude subdirectories exist
    claude_path.mkdir(parents=True, exist_ok=True)
    hooks_dir = claude_path / "hooks"
    skills_dir = claude_path / "skills"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)

    # Get source files
    install_dir = _get_install_dir()
    claude_install_dir = install_dir / "claude"
    install_hooks_dir = claude_install_dir / "hooks"
    install_skills_dir = claude_install_dir / "skills"

    # Hook files to copy
    hook_files = {
        "hook_dispatcher.py": True,  # Make executable
        "validate_settings.py": True,  # Make executable
        "README.md": False,
        "HOOK_SCHEMAS.md": False,
    }

    source_hooks_template = claude_install_dir / "hooks-template.json"

    # Verify all source files exist
    missing_files = []
    for filename in hook_files.keys():
        source_file = install_hooks_dir / filename
        if not source_file.exists():
            missing_files.append(str(source_file))

    if not source_hooks_template.exists():
        missing_files.append(str(source_hooks_template))

    if missing_files:
        result["error"] = f"Missing source files: {missing_files}"
        return result

    # Copy hook files
    for filename, make_executable in hook_files.items():
        source_file = install_hooks_dir / filename
        target_file = hooks_dir / filename

        if target_file.exists():
            target_file.unlink()

        copy2(source_file, target_file)
        if make_executable:
            target_file.chmod(0o755)

    # Copy skills
    if install_skills_dir.exists():
        import shutil

        for skill_dir in install_skills_dir.iterdir():
            if skill_dir.is_dir():
                target_skill_dir = skills_dir / skill_dir.name
                if target_skill_dir.exists():
                    shutil.rmtree(target_skill_dir)
                copytree(skill_dir, target_skill_dir)
                skills_installed.append(skill_dir.name)

    # Backup existing settings.json if it exists
    if settings_file.exists():
        timestamp = int(time.time())
        backup_file = claude_path / f"settings.json.{timestamp}.backup"
        copy2(settings_file, backup_file)

    # Load existing settings or create empty
    if settings_file.exists():
        with open(settings_file) as f:
            existing_settings = json.load(f)
    else:
        existing_settings = {}

    # Load Gobby hooks from template
    with open(source_hooks_template) as f:
        gobby_settings_str = f.read()

    # Replace $PROJECT_PATH with absolute project path
    abs_project_path = str(project_path.resolve())
    gobby_settings_str = gobby_settings_str.replace("$PROJECT_PATH", abs_project_path)
    gobby_settings = json.loads(gobby_settings_str)

    # Ensure hooks section exists
    if "hooks" not in existing_settings:
        existing_settings["hooks"] = {}

    # Merge Gobby hooks
    gobby_hooks = gobby_settings.get("hooks", {})
    for hook_type, hook_config in gobby_hooks.items():
        existing_settings["hooks"][hook_type] = hook_config
        hooks_installed.append(hook_type)

    # Write merged settings back
    with open(settings_file, "w") as f:
        json.dump(existing_settings, f, indent=2)

    result["success"] = True
    return result


def _install_gemini_hooks(project_path: Path) -> dict[str, Any]:
    """Install Gemini CLI hooks to a project.

    Args:
        project_path: Path to the project root

    Returns:
        Dict with installation results:
        - success: bool
        - hooks_installed: list of hook names
        - error: str (if success=False)
    """
    from shutil import copy2

    hooks_installed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "hooks_installed": hooks_installed,
        "error": None,
    }

    gemini_path = project_path / ".gemini"
    settings_file = gemini_path / "settings.json"

    # Ensure .gemini subdirectories exist
    gemini_path.mkdir(parents=True, exist_ok=True)
    hooks_dir = gemini_path / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    # Get source files
    install_dir = _get_install_dir()
    gemini_install_dir = install_dir / "gemini"
    install_hooks_dir = gemini_install_dir / "hooks"
    source_hooks_template = gemini_install_dir / "hooks-template.json"

    # Verify source files exist
    dispatcher_file = install_hooks_dir / "hook_dispatcher.py"
    if not dispatcher_file.exists():
        result["error"] = f"Missing hook dispatcher: {dispatcher_file}"
        return result

    if not source_hooks_template.exists():
        result["error"] = f"Missing hooks template: {source_hooks_template}"
        return result

    # Copy hook dispatcher
    target_dispatcher = hooks_dir / "hook_dispatcher.py"
    if target_dispatcher.exists():
        target_dispatcher.unlink()
    copy2(dispatcher_file, target_dispatcher)
    target_dispatcher.chmod(0o755)

    # Backup existing settings.json if it exists
    if settings_file.exists():
        timestamp = int(time.time())
        backup_file = gemini_path / f"settings.json.{timestamp}.backup"
        copy2(settings_file, backup_file)

    # Load existing settings or create empty
    if settings_file.exists():
        try:
            with open(settings_file) as f:
                existing_settings = json.load(f)
        except json.JSONDecodeError:
            # If invalid JSON, treat as empty but warn (backup already made)
            existing_settings = {}
    else:
        existing_settings = {}

    # Load Gobby hooks from template
    with open(source_hooks_template) as f:
        gobby_settings_str = f.read()

    # Resolve uv path dynamically to avoid PATH issues in Gemini CLI
    from shutil import which
    uv_path = which("uv")
    if not uv_path:
        uv_path = "uv"  # Fallback

    # Replace $PROJECT_PATH with absolute project path
    abs_project_path = str(project_path.resolve())

    # Replace variables in template
    gobby_settings_str = gobby_settings_str.replace("$PROJECT_PATH", abs_project_path)

    # Also replace "uv run python" with absolute path if found
    # The template uses "uv run python" by default
    if uv_path != "uv":
        gobby_settings_str = gobby_settings_str.replace("uv run python", f"{uv_path} run python")

    gobby_settings = json.loads(gobby_settings_str)

    # Ensure hooks section exists
    if "hooks" not in existing_settings:
        existing_settings["hooks"] = {}

    # Merge Gobby hooks (preserving any existing hooks)
    gobby_hooks = gobby_settings.get("hooks", {})
    for hook_type, hook_config in gobby_hooks.items():
        existing_settings["hooks"][hook_type] = hook_config
        hooks_installed.append(hook_type)

    # Crucially, ensure hooks are enabled in Gemini CLI
    if "general" not in existing_settings:
        existing_settings["general"] = {}
    existing_settings["general"]["enableHooks"] = True


    # Write merged settings back
    with open(settings_file, "w") as f:
        json.dump(existing_settings, f, indent=2)

    result["success"] = True
    return result


def _uninstall_claude_hooks(project_path: Path) -> dict[str, Any]:
    """Uninstall Claude Code hooks from a project.

    Args:
        project_path: Path to the project root

    Returns:
        Dict with uninstall results:
        - success: bool
        - hooks_removed: list of hook names
        - files_removed: list of filenames
        - skills_removed: list of skill names
        - error: str (if success=False)
    """
    import shutil

    hooks_removed: list[str] = []
    files_removed: list[str] = []
    skills_removed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "hooks_removed": hooks_removed,
        "files_removed": files_removed,
        "skills_removed": skills_removed,
        "error": None,
    }

    claude_path = project_path / ".claude"
    settings_file = claude_path / "settings.json"
    hooks_dir = claude_path / "hooks"
    skills_dir = claude_path / "skills"

    if not settings_file.exists():
        result["error"] = f"Settings file not found: {settings_file}"
        return result

    # Backup settings.json
    timestamp = int(time.time())
    backup_file = claude_path / f"settings.json.{timestamp}.backup"
    from shutil import copy2

    copy2(settings_file, backup_file)

    # Remove hooks from settings.json
    with open(settings_file) as f:
        settings = json.load(f)

    if "hooks" in settings:
        hook_types = [
            "SessionStart",
            "SessionEnd",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "PreCompact",
            "Notification",
            "Stop",
            "SubagentStart",
            "SubagentStop",
            "PermissionRequest",
        ]

        for hook_type in hook_types:
            if hook_type in settings["hooks"]:
                del settings["hooks"][hook_type]
                hooks_removed.append(hook_type)

        with open(settings_file, "w") as f:
            json.dump(settings, f, indent=2)

    # Remove hook files
    hook_files = [
        "hook_dispatcher.py",
        "validate_settings.py",
        "README.md",
        "HOOK_SCHEMAS.md",
    ]

    for filename in hook_files:
        file_path = hooks_dir / filename
        if file_path.exists():
            file_path.unlink()
            files_removed.append(filename)

    # Remove Gobby skills
    install_dir = _get_install_dir()
    install_skills_dir = install_dir / "claude" / "skills"

    if install_skills_dir.exists():
        for skill_dir in install_skills_dir.iterdir():
            if skill_dir.is_dir():
                target_skill_dir = skills_dir / skill_dir.name
                if target_skill_dir.exists():
                    shutil.rmtree(target_skill_dir)
                    skills_removed.append(skill_dir.name)

    result["success"] = True
    return result


def _uninstall_gemini_hooks(project_path: Path) -> dict[str, Any]:
    """Uninstall Gemini CLI hooks from a project.

    Args:
        project_path: Path to the project root

    Returns:
        Dict with uninstall results:
        - success: bool
        - hooks_removed: list of hook names
        - files_removed: list of filenames
        - error: str (if success=False)
    """
    hooks_removed: list[str] = []
    files_removed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "hooks_removed": hooks_removed,
        "files_removed": files_removed,
        "error": None,
    }

    gemini_path = project_path / ".gemini"
    settings_file = gemini_path / "settings.json"
    hooks_dir = gemini_path / "hooks"

    if not settings_file.exists():
        # No settings file means nothing to uninstall
        result["success"] = True
        return result

    # Backup settings.json
    timestamp = int(time.time())
    backup_file = gemini_path / f"settings.json.{timestamp}.backup"
    from shutil import copy2

    copy2(settings_file, backup_file)

    # Remove hooks from settings.json
    with open(settings_file) as f:
        settings = json.load(f)

    if "hooks" in settings:
        hook_types = [
            "SessionStart",
            "SessionEnd",
            "BeforeAgent",
            "AfterAgent",
            "BeforeTool",
            "AfterTool",
            "BeforeToolSelection",
            "BeforeModel",
            "AfterModel",
            "PreCompress",
            "Notification",
        ]

        for hook_type in hook_types:
            if hook_type in settings["hooks"]:
                del settings["hooks"][hook_type]
                hooks_removed.append(hook_type)

        # Also remove the "general" section if "enableHooks" was the only entry
        if "general" in settings and settings["general"].get("enableHooks") is True:
            # Check if there are other entries in "general"
            if len(settings["general"]) == 1:
                del settings["general"]
            else:
                del settings["general"]["enableHooks"]

        with open(settings_file, "w") as f:
            json.dump(settings, f, indent=2)

    # Remove hook dispatcher
    dispatcher_file = hooks_dir / "hook_dispatcher.py"
    if dispatcher_file.exists():
        dispatcher_file.unlink()
        files_removed.append("hook_dispatcher.py")

    # Attempt to remove empty hooks directory
    try:
        if hooks_dir.exists() and not any(hooks_dir.iterdir()):
            hooks_dir.rmdir()
    except Exception:
        pass

    result["success"] = True
    return result


def _install_codex_notify() -> dict[str, Any]:
    """Install Codex notify script and configure ~/.codex/config.toml.

    Codex does not use project-local hook directories. Instead, interactive Codex
    can be configured with a `notify = [...]` command in `~/.codex/config.toml`.

    Returns:
        Dict with installation results:
        - success: bool
        - files_installed: list of file paths
        - config_updated: bool
        - error: str (if success=False)
    """
    import json as _json
    import re
    from shutil import copy2

    files_installed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "files_installed": files_installed,
        "config_updated": False,
        "error": None,
    }

    install_dir = _get_install_dir()
    source_notify = install_dir / "codex" / "notify.py"
    if not source_notify.exists():
        result["error"] = f"Missing source file: {source_notify}"
        return result

    # Install notify script to ~/.gobby/hooks/codex/notify.py
    notify_dir = Path.home() / ".gobby" / "hooks" / "codex"
    notify_dir.mkdir(parents=True, exist_ok=True)
    target_notify = notify_dir / "notify.py"

    if target_notify.exists():
        target_notify.unlink()

    copy2(source_notify, target_notify)
    target_notify.chmod(0o755)
    files_installed.append(str(target_notify))

    # Update ~/.codex/config.toml
    codex_config_dir = Path.home() / ".codex"
    codex_config_dir.mkdir(parents=True, exist_ok=True)
    codex_config_path = codex_config_dir / "config.toml"

    notify_command = ["python3", str(target_notify)]
    notify_line = f"notify = {_json.dumps(notify_command)}"

    try:
        if codex_config_path.exists():
            existing = codex_config_path.read_text(encoding="utf-8")
        else:
            existing = ""

        pattern = re.compile(r"(?m)^\\s*notify\\s*=.*$")
        if pattern.search(existing):
            updated = pattern.sub(notify_line, existing)
        else:
            updated = (existing.rstrip() + "\n\n" if existing.strip() else "") + notify_line + "\n"

        if updated != existing:
            if codex_config_path.exists():
                backup_path = codex_config_path.with_suffix(".toml.bak")
                backup_path.write_text(existing, encoding="utf-8")

            codex_config_path.write_text(updated, encoding="utf-8")
            result["config_updated"] = True

        result["success"] = True
        return result

    except Exception as e:
        result["error"] = f"Failed to update Codex config: {e}"
        return result


def _uninstall_codex_notify() -> dict[str, Any]:
    """Uninstall Codex notify script and remove from ~/.codex/config.toml.

    Returns:
        Dict with uninstall results:
        - success: bool
        - files_removed: list of file paths
        - config_updated: bool
        - error: str (if success=False)
    """
    import re

    files_removed: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "files_removed": files_removed,
        "config_updated": False,
        "error": None,
    }

    # Remove notify script from ~/.gobby/hooks/codex/notify.py
    notify_file = Path.home() / ".gobby" / "hooks" / "codex" / "notify.py"
    if notify_file.exists():
        notify_file.unlink()
        files_removed.append(str(notify_file))

    # Try to remove empty parent directories
    notify_dir = notify_file.parent
    try:
        if notify_dir.exists() and not any(notify_dir.iterdir()):
            notify_dir.rmdir()
    except Exception:
        pass

    # Update ~/.codex/config.toml to remove notify line
    codex_config_path = Path.home() / ".codex" / "config.toml"

    try:
        if codex_config_path.exists():
            existing = codex_config_path.read_text(encoding="utf-8")

            # Remove notify = [...] line
            pattern = re.compile(r"(?m)^\s*notify\s*=.*$\n?")
            if pattern.search(existing):
                updated = pattern.sub("", existing)

                # Clean up multiple blank lines
                updated = re.sub(r"\n{3,}", "\n\n", updated)

                if updated != existing:
                    # Backup before modifying
                    backup_path = codex_config_path.with_suffix(".toml.bak")
                    backup_path.write_text(existing, encoding="utf-8")

                    codex_config_path.write_text(updated, encoding="utf-8")
                    result["config_updated"] = True

        result["success"] = True
        return result

    except Exception as e:
        result["error"] = f"Failed to update Codex config: {e}"
        return result


@cli.command("install")
@click.option(
    "--claude",
    "install_claude",
    is_flag=True,
    help="Install Claude Code hooks only",
)
@click.option(
    "--gemini",
    "install_gemini",
    is_flag=True,
    help="Install Gemini CLI hooks only",
)
@click.option(
    "--codex",
    "install_codex",
    is_flag=True,
    help="Configure Codex notify integration (interactive Codex)",
)
@click.option(
    "--all",
    "install_all",
    is_flag=True,
    default=False,
    help="Install hooks for all detected CLIs (default behavior when no flags specified)",
)
def install(install_claude: bool, install_gemini: bool, install_codex: bool, install_all: bool) -> None:
    """Install Gobby hooks to AI coding CLIs.

    By default (no flags), installs to all detected CLIs.
    Use --claude, --gemini, or --codex to install only to specific CLIs.

    Installs to project-level directories in current working directory.
    """
    project_path = Path.cwd()

    # Determine which CLIs to install
    # If no flags specified, act like --all
    if not install_claude and not install_gemini and not install_all:
        install_all = True

    codex_detected = _is_codex_cli_installed()

    # Build list of CLIs to install
    clis_to_install = []

    if install_all:
        # Auto-detect installed CLIs
        if _is_claude_code_installed():
            clis_to_install.append("claude")
        if _is_gemini_cli_installed():
            clis_to_install.append("gemini")
        if codex_detected:
            clis_to_install.append("codex")

        if not clis_to_install:
            click.echo("No supported AI coding CLIs detected.")
            click.echo("\nSupported CLIs:")
            click.echo("  - Claude Code: npm install -g @anthropic-ai/claude-code")
            click.echo("  - Gemini CLI:  npm install -g @google/gemini-cli")
            click.echo("  - Codex CLI:   npm install -g @openai/codex")
            click.echo("\nYou can still install manually with --claude, --gemini, or --codex flags.")
            sys.exit(1)
    else:
        if install_claude:
            clis_to_install.append("claude")
        if install_gemini:
            clis_to_install.append("gemini")
        if install_codex:
            clis_to_install.append("codex")

    # Get install directory info
    install_dir = _get_install_dir()
    is_dev_mode = "src" in str(install_dir)

    click.echo("=" * 60)
    click.echo("  Gobby Hooks Installation")
    click.echo("=" * 60)
    click.echo(f"\nProject: {project_path}")
    if is_dev_mode:
        click.echo("Mode: Development (using source directory)")
    click.echo(f"CLIs to configure: {', '.join(clis_to_install)}")
    click.echo("")

    # Track results
    results = {}

    # Install Claude Code hooks
    if "claude" in clis_to_install:
        click.echo("-" * 40)
        click.echo("Claude Code")
        click.echo("-" * 40)

        result = _install_claude_hooks(project_path)
        results["claude"] = result

        if result["success"]:
            click.echo(f"Installed {len(result['hooks_installed'])} hooks")
            for hook in result["hooks_installed"]:
                click.echo(f"  - {hook}")
            if result["skills_installed"]:
                click.echo(f"Installed {len(result['skills_installed'])} skills")
                for skill in result["skills_installed"]:
                    click.echo(f"  - {skill}")
            click.echo(f"Configuration: {project_path / '.claude' / 'settings.json'}")
        else:
            click.echo(f"Failed: {result['error']}", err=True)
        click.echo("")

    # Install Gemini CLI hooks
    if "gemini" in clis_to_install:
        click.echo("-" * 40)
        click.echo("Gemini CLI")
        click.echo("-" * 40)

        result = _install_gemini_hooks(project_path)
        results["gemini"] = result

        if result["success"]:
            click.echo(f"Installed {len(result['hooks_installed'])} hooks")
            for hook in result["hooks_installed"]:
                click.echo(f"  - {hook}")
            click.echo(f"Configuration: {project_path / '.gemini' / 'settings.json'}")
        else:
            click.echo(f"Failed: {result['error']}", err=True)
        click.echo("")

    # Configure Codex notify integration (interactive Codex)
    if "codex" in clis_to_install:
        click.echo("-" * 40)
        click.echo("Codex")
        click.echo("-" * 40)

        if not codex_detected:
            click.echo("Codex CLI not detected in PATH (`codex`).", err=True)
            click.echo("Install Codex first, then re-run:")
            click.echo("  npm install -g @openai/codex\n")
            results["codex"] = {"success": False, "error": "Codex CLI not detected"}
        else:
            result = _install_codex_notify()
            results["codex"] = result

            if result["success"]:
                click.echo("Installed Codex notify integration")
                for file_path in result["files_installed"]:
                    click.echo(f"  - {file_path}")
                if result.get("config_updated"):
                    click.echo("Updated: ~/.codex/config.toml (set `notify = ...`)")
                else:
                    click.echo("~/.codex/config.toml already configured")
            else:
                click.echo(f"Failed: {result['error']}", err=True)
        click.echo("")

    # Summary
    click.echo("=" * 60)
    click.echo("  Summary")
    click.echo("=" * 60)

    all_success = all(r.get("success", False) for r in results.values())

    if all_success:
        click.echo("\nInstallation completed successfully!")
    else:
        failed = [cli for cli, r in results.items() if not r.get("success", False)]
        click.echo(f"\nSome installations failed: {', '.join(failed)}")

    click.echo("\nNext steps:")
    click.echo("  1. Ensure the Gobby daemon is running:")
    click.echo("     gobby start")
    click.echo("  2. Start a new session in your AI coding CLI")
    click.echo("  3. Your sessions will now be tracked locally")

    if not all_success:
        sys.exit(1)


@cli.command("uninstall")
@click.option(
    "--claude",
    "uninstall_claude",
    is_flag=True,
    help="Uninstall Claude Code hooks only",
)
@click.option(
    "--gemini",
    "uninstall_gemini",
    is_flag=True,
    help="Uninstall Gemini CLI hooks only",
)
@click.option(
    "--codex",
    "uninstall_codex",
    is_flag=True,
    help="Uninstall Codex notify integration",
)
@click.option(
    "--all",
    "uninstall_all",
    is_flag=True,
    default=False,
    help="Uninstall hooks from all CLIs (default behavior when no flags specified)",
)
@click.confirmation_option(prompt="Are you sure you want to uninstall Gobby hooks?")
def uninstall(uninstall_claude: bool, uninstall_gemini: bool, uninstall_codex: bool, uninstall_all: bool) -> None:
    """Uninstall Gobby hooks from AI coding CLIs.

    By default (no flags), uninstalls from all CLIs that have hooks installed.
    Use --claude, --gemini, or --codex to uninstall only from specific CLIs.

    Uninstalls from project-level directories in current working directory.
    """
    project_path = Path.cwd()

    # Determine which CLIs to uninstall
    # If no flags specified, act like --all
    if not uninstall_claude and not uninstall_gemini and not uninstall_codex and not uninstall_all:
        uninstall_all = True

    # Build list of CLIs to uninstall
    clis_to_uninstall = []

    if uninstall_all:
        # Check which CLIs have hooks installed
        claude_settings = project_path / ".claude" / "settings.json"
        gemini_settings = project_path / ".gemini" / "settings.json"
        codex_notify = Path.home() / ".gobby" / "hooks" / "codex" / "notify.py"

        if claude_settings.exists():
            clis_to_uninstall.append("claude")
        if gemini_settings.exists():
            clis_to_uninstall.append("gemini")
        if codex_notify.exists():
            clis_to_uninstall.append("codex")

        if not clis_to_uninstall:
            click.echo("No Gobby hooks found to uninstall.")
            click.echo(f"\nChecked: {project_path / '.claude'}")
            click.echo(f"         {project_path / '.gemini'}")
            click.echo(f"         {codex_notify}")
            sys.exit(0)
    else:
        if uninstall_claude:
            clis_to_uninstall.append("claude")
        if uninstall_gemini:
            clis_to_uninstall.append("gemini")
        if uninstall_codex:
            clis_to_uninstall.append("codex")

    click.echo("=" * 60)
    click.echo("  Gobby Hooks Uninstallation")
    click.echo("=" * 60)
    click.echo(f"\nProject: {project_path}")
    click.echo(f"CLIs to uninstall from: {', '.join(clis_to_uninstall)}")
    click.echo("")

    # Track results
    results = {}

    # Uninstall Claude Code hooks
    if "claude" in clis_to_uninstall:
        click.echo("-" * 40)
        click.echo("Claude Code")
        click.echo("-" * 40)

        result = _uninstall_claude_hooks(project_path)
        results["claude"] = result

        if result["success"]:
            if result["hooks_removed"]:
                click.echo(f"Removed {len(result['hooks_removed'])} hooks from settings")
                for hook in result["hooks_removed"]:
                    click.echo(f"  - {hook}")
            if result["files_removed"]:
                click.echo(f"Removed {len(result['files_removed'])} files")
            if result["skills_removed"]:
                click.echo(f"Removed {len(result['skills_removed'])} skills")
            if not result["hooks_removed"] and not result["files_removed"]:
                click.echo("  (no hooks found to remove)")
        else:
            click.echo(f"Failed: {result['error']}", err=True)
        click.echo("")

    # Uninstall Gemini CLI hooks
    if "gemini" in clis_to_uninstall:
        click.echo("-" * 40)
        click.echo("Gemini CLI")
        click.echo("-" * 40)

        result = _uninstall_gemini_hooks(project_path)
        results["gemini"] = result

        if result["success"]:
            if result["hooks_removed"]:
                click.echo(f"Removed {len(result['hooks_removed'])} hooks from settings")
                for hook in result["hooks_removed"]:
                    click.echo(f"  - {hook}")
            if result["files_removed"]:
                click.echo(f"Removed {len(result['files_removed'])} files")
            if not result["hooks_removed"] and not result["files_removed"]:
                click.echo("  (no hooks found to remove)")
        else:
            click.echo(f"Failed: {result['error']}", err=True)
        click.echo("")

    # Uninstall Codex notify integration
    if "codex" in clis_to_uninstall:
        click.echo("-" * 40)
        click.echo("Codex")
        click.echo("-" * 40)

        result = _uninstall_codex_notify()
        results["codex"] = result

        if result["success"]:
            if result["files_removed"]:
                click.echo(f"Removed {len(result['files_removed'])} files")
                for f in result["files_removed"]:
                    click.echo(f"  - {f}")
            if result.get("config_updated"):
                click.echo("Updated: ~/.codex/config.toml (removed `notify = ...`)")
            if not result["files_removed"] and not result.get("config_updated"):
                click.echo("  (no codex integration found to remove)")
        else:
            click.echo(f"Failed: {result['error']}", err=True)
        click.echo("")

    # Summary
    click.echo("=" * 60)
    click.echo("  Summary")
    click.echo("=" * 60)

    all_success = all(r.get("success", False) for r in results.values())

    if all_success:
        click.echo("\nUninstallation completed successfully!")
    else:
        failed = [cli for cli, r in results.items() if not r.get("success", False)]
        click.echo(f"\nSome uninstallations failed: {', '.join(failed)}")

    if not all_success:
        sys.exit(1)


if __name__ == "__main__":
    cli()
