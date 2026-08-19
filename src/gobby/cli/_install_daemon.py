"""Daemon startup helpers for the install command."""

import os
import shutil
import socket
import subprocess  # nosec B404 # fixed daemon startup command
import sys
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import click

from gobby.cli.installers.remote_preflight import run_remote_preflight
from gobby.config.bootstrap import (
    DEFAULT_DAEMON_PORT,
    DEFAULT_WEBSOCKET_PORT,
    BootstrapConfigError,
    DatastoreMode,
    load_bootstrap,
)
from gobby.utils.dependency_requirements import (
    collect_dependency_report,
    required_dependency_errors,
    unsupported_platform_error,
)


def _is_source_checkout_install(install_dir: Path) -> bool:
    resolved = install_dir.expanduser().resolve()
    for candidate in (resolved, *resolved.parents):
        install_package = candidate / "src" / "gobby" / "install"
        if install_package.is_dir() and (
            (candidate / "pyproject.toml").is_file() or (candidate / ".git").exists()
        ):
            return True
    return False


def _docker_daemon_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(  # nosec B603 B607
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _port_available(port: int, host: str = "0.0.0.0") -> bool:  # nosec B104
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(0.2)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _run_install_preflight(
    *,
    is_full_install: bool,
    install_dir: Path,
    embedding_url: str | None,
    embedding_provider: str | None,
    managed_services: bool = False,
    datastore_mode: DatastoreMode = "local",
    database_url: str | None = None,
    gobby_home: Path | None = None,
    hub_daemon_url: str | None = None,
) -> tuple[list[str], list[str]]:
    """Return full-install preflight errors and optional warnings."""
    errors: list[str] = []
    warnings: list[str] = []

    if platform_error := unsupported_platform_error():
        errors.append(platform_error)
    dependency_report = collect_dependency_report(
        managed_services=managed_services and datastore_mode == "local",
        include_srt=False,
    )
    errors.extend(required_dependency_errors(dependency_report))

    if is_full_install:
        if datastore_mode == "remote":
            if database_url:
                errors.extend(
                    run_remote_preflight(
                        database_url,
                        gobby_home=gobby_home,
                        hub_daemon_url=hub_daemon_url,
                    )
                )
            else:
                errors.append(
                    "Remote datastore install requires database_url in bootstrap.yaml. "
                    "Copy the hub database_url and retry."
                )
        elif not _docker_daemon_available():
            errors.append("Docker daemon is required for full install. Start Docker and retry.")
        if _is_source_checkout_install(install_dir) and shutil.which("uv") is None:
            errors.append("uv is required when installing from a source checkout.")

        if not embedding_url and not embedding_provider:
            warnings.append(
                "No embedding provider override supplied; install will prompt or keep "
                "semantic features disabled."
            )

    if is_full_install:
        for port in (DEFAULT_DAEMON_PORT, DEFAULT_WEBSOCKET_PORT):
            if not _port_available(port):
                warnings.append(f"Port {port} is already in use.")

    return errors, warnings


def _headless_or_remote() -> bool:
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return True
    if sys.platform.startswith("linux"):
        return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return False


def _ci_environment() -> bool:
    return any(os.environ.get(name) for name in ("CI", "GITHUB_ACTIONS", "BUILDKITE"))


def _daemon_url() -> str:
    try:
        port = load_bootstrap(resolve_database_url=False).daemon_port
    except (BootstrapConfigError, FileNotFoundError, PermissionError, OSError, ValueError):
        port = DEFAULT_DAEMON_PORT
    return f"http://localhost:{port}/"


def _daemon_already_running() -> bool:
    try:
        from gobby.cli.daemon import _is_daemon_healthy

        port = urlparse(_daemon_url()).port
        if port is None:
            return False
        return _is_daemon_healthy(port)
    except (ConnectionError, OSError, ValueError):
        return False


def maybe_start_daemon_after_install(
    *,
    no_interactive: bool,
    daemon_url: Callable[[], str] = _daemon_url,
    daemon_already_running: Callable[[], bool] = _daemon_already_running,
    ci_environment: Callable[[], bool] = _ci_environment,
    headless_or_remote: Callable[[], bool] = _headless_or_remote,
    subprocess_popen: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
    browser_open: Callable[[str], bool] = webbrowser.open,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    claim: Any | None = None,
) -> None:
    """Start the daemon after install when an interactive local UI is available."""
    url = daemon_url()
    if no_interactive or ci_environment() or headless_or_remote():
        click.echo(f"Gobby UI: {url}")
        click.echo("Run `/gobby intro` in your first agent session.")
        return
    if daemon_already_running():
        click.echo(f"Gobby daemon already running: {url}")
        click.echo("Run `/gobby intro` in your first agent session.")
        return

    click.echo("Starting Gobby daemon...")
    popen_kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "start_new_session": True,
    }
    if claim is not None:
        from gobby.cli.daemon_singleton import service_backend_name
        from gobby.cli.installers.service import get_service_status, service_start
        from gobby.runner_pid_file import (
            SingletonReservationError,
            convert_held_claim_to_reservation,
        )

        svc = get_service_status()
        if svc.get("installed"):
            platform = svc.get("platform")
            backend = service_backend_name(platform if isinstance(platform, str) else None)
            try:
                convert_held_claim_to_reservation(claim, backend=backend)
            except SingletonReservationError as exc:
                click.echo(f"Warning: failed to convert install singleton: {exc}")
                click.echo(f"Start manually with `gobby start`, then open {url}")
                return
            result = service_start(reserved=True)
            if not result.get("success"):
                click.echo(f"Warning: failed to start daemon automatically: {result.get('error')}")
                click.echo(f"Start manually with `gobby start`, then open {url}")
            return
        env = os.environ.copy()
        env.update(claim.inherit_environment())
        popen_kwargs["env"] = env
        if os.name == "posix":
            popen_kwargs["close_fds"] = True
            popen_kwargs["pass_fds"] = (claim.fileno(),)
    try:
        process = subprocess_popen(  # nosec B603 # command uses current interpreter/module
            [sys.executable, "-m", "gobby.cli", "start"],
            **popen_kwargs,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        click.echo(f"Warning: failed to start daemon automatically: {exc}")
        click.echo(f"Start manually with `gobby start`, then open {url}")
        return

    deadline = monotonic() + 5
    while monotonic() < deadline:
        if daemon_already_running():
            if claim is not None:
                claim.detach()
            click.echo(f"Gobby daemon started: {url}")
            if not browser_open(url):
                click.echo(f"Open {url}")
            click.echo("Run `/gobby intro` in your first agent session.")
            return
        returncode = process.poll()
        if returncode is not None:
            if returncode != 0:
                click.echo(f"Warning: failed to start daemon automatically: exited {returncode}")
                click.echo(f"Start manually with `gobby start`, then open {url}")
                return
            break
        sleep(0.25)

    if daemon_already_running():
        if claim is not None:
            claim.detach()
        click.echo(f"Gobby daemon started: {url}")
        if not browser_open(url):
            click.echo(f"Open {url}")
    else:
        click.echo("Warning: daemon did not become healthy automatically.")
        click.echo(f"Start manually with `gobby start`, then open {url}")
    click.echo("Run `/gobby intro` in your first agent session.")
