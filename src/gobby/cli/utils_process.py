"""Process, logging, and port helpers for CLI utilities."""

from __future__ import annotations

import logging
import os
import signal
import socket
import time

import click
import psutil

from gobby.cli.utils_runtime import facade
from gobby.config.bootstrap import DEFAULT_WEBSOCKET_PORT


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for CLI."""
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def format_uptime(seconds: float) -> str:
    """Format uptime in human-readable format."""
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
    """Check if a port is available for binding."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        sock.bind((host, port))
        sock.close()
        return True
    except OSError:
        sock.close()
        return False


def get_port_listener_pid(port: int) -> int | None:
    """Return the PID owning a TCP listener on ``port``, if observable."""
    try:
        for proc in psutil.process_iter(["pid"]):
            try:
                if proc.status() in (psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE):
                    continue
                for conn in proc.net_connections(kind="tcp"):
                    if (
                        conn.status == psutil.CONN_LISTEN
                        and getattr(conn.laddr, "port", None) == port
                    ):
                        return int(proc.pid)
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
    except (OSError, psutil.Error):
        return None
    return None


def wait_for_port_available(port: int, host: str = "localhost", timeout: float = 5.0) -> bool:
    """Wait for a port to become available."""
    deps = facade()
    start_time = time.time()

    while (time.time() - start_time) < timeout:
        if bool(deps.is_port_available(port, host)):
            return True
        time.sleep(0.1)

    return False


def kill_all_gobby_daemons() -> int:
    """Find and kill all gobby daemon processes."""
    deps = facade()

    if os.environ.get("GOBBY_TEST_PROTECT", "").lower() in ("1", "true", "yes"):
        deps.logger.warning("kill_all_gobby_daemons called during test - skipping")
        return 0

    try:
        config = deps.load_config()
        http_port = int(config.daemon_port)
        ws_port = int(config.websocket.port)
    except (AttributeError, OSError, TypeError, ValueError):
        http_port = 60887
        ws_port = DEFAULT_WEBSOCKET_PORT

    killed_count = 0
    current_pid = os.getpid()
    parent_pid = os.getppid()
    pid_file_pid: int | None = None
    try:
        pid_file_text = (deps.get_gobby_home() / "gobby.pid").read_text().strip()
        pid_file_pid = int(pid_file_text) if pid_file_text else None
    except (OSError, TypeError, ValueError):
        pid_file_pid = None

    parent_pids = {current_pid, parent_pid}
    try:
        parent_proc = psutil.Process(parent_pid)
        while parent_proc.parent() is not None:
            parent_proc = parent_proc.parent()
            parent_pids.add(parent_proc.pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.pid in parent_pids:
                continue

            cmdline = proc.cmdline()
            cmdline_str = " ".join(cmdline)
            cmdline_lower = cmdline_str.lower()
            process_name = str(proc.info.get("name") or "").lower()
            has_gobby_daemon_marker = (
                "gobby.runner" in cmdline_str
                or "gobby_client.runner" in cmdline_str
                or ("gobby" in process_name and "daemon" in cmdline_lower)
            )
            owns_daemon_port = False
            try:
                connections = proc.net_connections()
                for conn in connections:
                    if hasattr(conn, "laddr") and conn.laddr:
                        if conn.laddr.port in [http_port, ws_port]:
                            owns_daemon_port = True
                            break
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass

            has_pid_file_identity = pid_file_pid == proc.pid and owns_daemon_port

            is_gobby_daemon = (
                (has_gobby_daemon_marker or has_pid_file_identity)
                and "gobby.cli" not in cmdline_str
                and "gobby_client.cli" not in cmdline_str
            )

            if not is_gobby_daemon:
                is_gobby_daemon = owns_daemon_port and has_gobby_daemon_marker

            if is_gobby_daemon:
                click.echo(f"Found gobby daemon (PID {proc.pid}): {cmdline_str[:100]}")

                from gobby.runner_maintenance import write_shutdown_source
                from gobby.shutdown_intent import ShutdownIntent

                try:
                    write_shutdown_source("cli_kill_all", intent=ShutdownIntent.STOP)
                    proc.send_signal(signal.SIGTERM)
                    proc.wait(timeout=5)
                    click.echo(f"Gracefully stopped PID {proc.pid}")
                    killed_count += 1
                except psutil.TimeoutExpired:
                    click.echo(f"Process {proc.pid} didn't stop gracefully, force killing...")
                    try:
                        proc.kill()
                        proc.wait(timeout=2)
                        click.echo(f"Force killed PID {proc.pid}")
                    except psutil.TimeoutExpired:
                        click.echo(
                            f"Warning: PID {proc.pid} did not exit after SIGKILL",
                            err=True,
                        )
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                    killed_count += 1

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
        except OSError as exc:
            click.echo(f"Warning: Error checking process {proc.pid}: {exc}", err=True)

    return killed_count


def _is_process_alive(pid: int) -> bool:
    """Check if a process is truly alive (not zombie, not dead)."""
    try:
        proc = psutil.Process(pid)
        return bool(proc.status() != psutil.STATUS_ZOMBIE)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _kill_port_holder(port: int) -> None:
    """Kill any process listening on the given port."""
    deps = facade()

    if os.environ.get("GOBBY_TEST_PROTECT", "").lower() in ("1", "true", "yes"):
        deps.logger.warning("_kill_port_holder called during test - skipping")
        return

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = proc.cmdline()
            cmdline_str = " ".join(cmdline)
            cmdline_lower = cmdline_str.lower()
            process_name = str(proc.info.get("name") or "").lower()
            has_gobby_daemon_marker = (
                "gobby.runner" in cmdline_str
                or "gobby_client.runner" in cmdline_str
                or ("gobby" in process_name and "daemon" in cmdline_lower)
            )
            if not has_gobby_daemon_marker:
                continue

            for conn in proc.net_connections():
                if (
                    hasattr(conn, "laddr")
                    and conn.laddr
                    and conn.laddr.port == port
                    and conn.status == psutil.CONN_LISTEN
                ):
                    deps.logger.info(
                        "Killing orphan process on port %s: PID %s (%s)",
                        port,
                        proc.pid,
                        proc.name(),
                    )
                    parent = psutil.Process(proc.pid)
                    children = parent.children(recursive=True)
                    for child in children:
                        child.terminate()
                    parent.terminate()
                    _, alive = psutil.wait_procs([parent] + children, timeout=3)
                    for process in alive:
                        process.kill()
                    return
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
