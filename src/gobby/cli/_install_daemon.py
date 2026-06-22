"""Daemon startup helpers for the install command."""

import os
import subprocess  # nosec B404 # fixed daemon startup command
import sys
import time
import webbrowser
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import click

from gobby.config.bootstrap import DEFAULT_DAEMON_PORT, BootstrapConfigError


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
        from gobby.config.app import load_config

        port = load_config(resolve_database_url=False).daemon_port
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
    try:
        process = subprocess_popen(  # nosec B603 # command uses current interpreter/module
            [sys.executable, "-m", "gobby.cli", "start"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        click.echo(f"Warning: failed to start daemon automatically: {exc}")
        click.echo(f"Start manually with `gobby start`, then open {url}")
        return

    deadline = monotonic() + 5
    while monotonic() < deadline:
        if daemon_already_running():
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
        click.echo(f"Gobby daemon started: {url}")
        if not browser_open(url):
            click.echo(f"Open {url}")
    else:
        click.echo("Warning: daemon did not become healthy automatically.")
        click.echo(f"Start manually with `gobby start`, then open {url}")
    click.echo("Run `/gobby intro` in your first agent session.")
