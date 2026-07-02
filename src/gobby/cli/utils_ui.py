"""Web UI discovery and lifecycle helpers for CLI utilities."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess  # nosec B404
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import cast

import click
import psutil

from gobby.cli.utils_runtime import facade
from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import DEFAULT_WEBSOCKET_PORT
from gobby.config.ui import UIConfig
from gobby.utils.dev import is_dev_mode


def _read_ui_pid_record(pid_file: Path) -> tuple[int, float | None]:
    raw = pid_file.read_text(encoding="utf-8").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return int(raw), None
    if not isinstance(data, dict):
        raise ValueError("UI PID file must contain an object")
    pid = data.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        raise ValueError("UI PID file is missing an integer pid")
    started_at = data.get("started_at")
    if started_at is not None and not isinstance(started_at, int | float):
        raise ValueError("UI PID file started_at must be numeric")
    return pid, float(started_at) if started_at is not None else None


def _write_ui_pid_record(pid_file: Path, process: subprocess.Popen[bytes]) -> None:
    try:
        proc = psutil.Process(process.pid)
        started_at = proc.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        started_at = None
    pid_file.write_text(
        json.dumps({"pid": process.pid, "started_at": started_at}),
        encoding="utf-8",
    )


def _process_start_matches(proc: psutil.Process, started_at: float | None) -> bool:
    if started_at is None:
        return True
    try:
        return bool(abs(proc.create_time() - started_at) < 1.0)
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return False


def find_web_dir(
    config: DaemonConfig | None = None, *, require_source: bool = False
) -> Path | None:
    """Find the web UI directory."""
    deps = facade()

    ui_config: UIConfig | object | None = (
        getattr(config, "ui", None) if config is not None else None
    )
    source_required = require_source or getattr(ui_config, "mode", None) == "dev"

    def _qualifies(path: Path) -> bool:
        if not path.exists():
            return False
        if (path / "package.json").exists():
            return True
        if not source_required and (path / "dist" / "index.html").exists():
            return True
        return False

    web_dir = getattr(ui_config, "web_dir", None)
    if web_dir and isinstance(web_dir, (str, os.PathLike)):
        configured_path = cast(Path, deps.Path(web_dir).expanduser())
        if _qualifies(configured_path):
            return configured_path

    try:
        cwd = cast(Path, deps.Path.cwd())
    except OSError as exc:
        deps.logger.debug("Could not resolve cwd for web UI discovery: %s", exc)
    else:
        source_web = cwd / "web"
        if is_dev_mode(cwd) and _qualifies(source_web):
            return source_web

    try:
        import gobby

        pkg_web = Path(gobby.__file__).parent / "ui" / "web"
        if _qualifies(pkg_web):
            return pkg_web
    except ImportError:
        deps.logger.debug("gobby package not importable, skipping package web dir")
    except OSError as exc:
        deps.logger.debug("Could not locate package web directory: %s", exc)

    return None


def _open_ui_log_handler(log_file: Path) -> RotatingFileHandler:
    """Open the UI log target with size-bounded rotation."""
    deps = facade()

    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_file,
        mode="a",
        maxBytes=deps._UI_LOG_MAX_BYTES,
        backupCount=deps._UI_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    rollover_probe = logging.LogRecord(
        name=__name__,
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="",
        args=(),
        exc_info=None,
    )
    if handler.shouldRollover(rollover_probe):
        handler.doRollover()
    return handler


def _is_gobby_ui_process(proc: psutil.Process, *, web_dir: Path | None = None) -> bool:
    try:
        cmdline = proc.cmdline()
        cwd = Path(proc.cwd()).resolve()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return False

    if web_dir is not None and cwd != web_dir.resolve():
        return False

    cmdline_lower = " ".join(cmdline).lower()
    npm_dev = "npm" in cmdline_lower and "run" in cmdline_lower and "dev" in cmdline_lower
    return npm_dev or "vite" in cmdline_lower


def _find_gobby_ui_port_holder(port: int, web_dir: Path) -> psutil.Process | None:
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if not _is_gobby_ui_process(proc, web_dir=web_dir):
                continue
            for conn in proc.net_connections(kind="inet"):
                if (
                    hasattr(conn, "laddr")
                    and conn.laddr
                    and conn.laddr.port == port
                    and conn.status == psutil.CONN_LISTEN
                ):
                    return proc
        except (psutil.Error, OSError):
            continue
    return None


def _terminate_ui_process(proc: psutil.Process) -> None:
    try:
        children = proc.children(recursive=True)
    except (psutil.Error, OSError):
        children = []
    try:
        proc.terminate()
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return
    except (psutil.Error, OSError):
        pass
    try:
        _, alive = psutil.wait_procs([proc] + children, timeout=3)
    except (psutil.Error, OSError):
        alive = children
    for process in alive:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except (psutil.Error, OSError):
            continue


def spawn_ui_server(
    host: str,
    port: int,
    web_dir: Path,
    log_file: Path,
    daemon_port: int = 60887,
    ws_port: int = DEFAULT_WEBSOCKET_PORT,
) -> int | None:
    """Spawn the UI dev server as a detached subprocess."""
    deps = facade()

    deps.stop_ui_server(quiet=True)

    if not bool(deps.is_port_available(port, host="0.0.0.0")):  # nosec B104
        port_holder = _find_gobby_ui_port_holder(port, web_dir)
        if port_holder is None:
            deps.logger.error(
                "Port %s is in use by a non-Gobby UI process; aborting UI server spawn",
                port,
            )
            return None
        deps.logger.info(
            "Stopping existing Gobby UI process on port %s: PID %s", port, port_holder.pid
        )
        _terminate_ui_process(port_holder)
        if not bool(
            deps.wait_for_port_available(port, host="0.0.0.0", timeout=5.0)  # nosec B104
        ):
            deps.logger.error("Port %s still in use after cleanup - aborting UI server spawn", port)
            return None

    node_modules = web_dir / "node_modules"
    if not node_modules.exists():
        deps.logger.debug("Installing web UI dependencies...")
        try:
            result = subprocess.run(  # nosec B603 B607
                ["npm", "install"],
                cwd=web_dir,
                capture_output=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            deps.logger.error("npm install timed out after 120s")
            return None
        except FileNotFoundError:
            deps.logger.error("npm not found - install Node.js/npm and ensure it is on PATH")
            return None

        if result.returncode != 0:
            deps.logger.error("Failed to install UI dependencies: %s", result.stderr.decode())
            return None

    cmd = ["npm", "run", "dev", "--", "--host", host, "--port", str(port)]

    try:
        log_handler = deps._open_ui_log_handler(log_file)
        try:
            log_stream = log_handler.stream
            if log_stream is None:
                raise RuntimeError(f"Failed to open UI log stream: {log_file}")
            env = os.environ.copy()
            env["GOBBY_DAEMON_PORT"] = str(daemon_port)
            env["GOBBY_WS_PORT"] = str(ws_port)
            env["GOBBY_UI_HOST"] = host
            process = subprocess.Popen(  # nosec B603 B607
                cmd,
                cwd=web_dir,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
        finally:
            log_handler.close()

        time.sleep(1.0)

        if process.poll() is not None:
            deps.logger.error(
                "UI server process exited immediately with code %s. Check logs: %s",
                process.returncode,
                log_file,
            )
            return None

        pid_file = cast(Path, deps.get_gobby_home() / "ui.pid")
        _write_ui_pid_record(pid_file, process)

        return int(process.pid)

    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        deps.logger.error("Failed to spawn UI server: %s", exc)
        return None


def stop_ui_server(quiet: bool = False) -> bool:
    """Stop the UI dev server. Returns True on success, False on failure."""
    deps = facade()

    if os.environ.get("GOBBY_TEST_PROTECT", "").lower() in ("1", "true", "yes"):
        deps.logger.warning("stop_ui_server called during test - skipping")
        return True

    pid_file = cast(Path, deps.get_gobby_home() / "ui.pid")

    if not pid_file.exists():
        if not quiet:
            deps.logger.debug("UI server not running (no PID file)")
        return True

    try:
        pid, started_at = _read_ui_pid_record(pid_file)
    except (OSError, ValueError) as exc:
        if not quiet:
            deps.logger.debug("Error reading UI PID file: %s", exc)
        pid_file.unlink(missing_ok=True)
        return True

    if not bool(deps._is_process_alive(pid)):
        if not quiet:
            deps.logger.debug("UI server not running (stale PID file with PID %s)", pid)
        pid_file.unlink(missing_ok=True)
        return True

    try:
        web_dir = find_web_dir(require_source=True)
        parent = psutil.Process(pid)
        if not _process_start_matches(parent, started_at):
            if not quiet:
                deps.logger.warning(
                    "Refusing to stop PID %s from ui.pid: process start time does not match",
                    pid,
                )
            return False
        if not _is_gobby_ui_process(parent, web_dir=web_dir):
            if not quiet:
                deps.logger.warning(
                    "Refusing to stop PID %s from ui.pid: process identity does not match Gobby UI",
                    pid,
                )
            return False
        try:
            children = parent.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pid_file.unlink(missing_ok=True)
            return True

        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pid_file.unlink(missing_ok=True)
            return True
        if not quiet:
            click.echo(f"Stopping UI server (PID {pid})")

        max_wait = 5
        for _ in range(max_wait * 10):
            time.sleep(0.1)
            if not bool(deps._is_process_alive(pid)):
                break

        for child in children:
            try:
                if child.is_running():
                    child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass

        if bool(deps._is_process_alive(pid)):
            try:
                os.kill(pid, signal.SIGKILL)
                time.sleep(0.5)
            except (ProcessLookupError, PermissionError):
                pass

        pid_file.unlink(missing_ok=True)
        return True

    except (ProcessLookupError, psutil.NoSuchProcess):
        pid_file.unlink(missing_ok=True)
        return True
    except (OSError, psutil.Error) as exc:
        if not quiet:
            deps.logger.debug("Error stopping UI server: %s", exc)
        return False


def _stop_step(msg: str, *, error: bool = False) -> None:
    """Print a shutdown step with consistent formatting."""
    if error:
        click.echo(f"  ! {msg}", err=True)
    else:
        click.echo(f"  + {msg}")
