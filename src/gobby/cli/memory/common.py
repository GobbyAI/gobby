from __future__ import annotations

from typing import TYPE_CHECKING

import click

from gobby.cli.utils_config import get_daemon_client
from gobby.config.app import DaemonConfig

if TYPE_CHECKING:
    from gobby.utils.daemon_client import DaemonClient


def _get_daemon_client(ctx: click.Context) -> DaemonClient:
    """Get a DaemonClient for calling daemon HTTP API."""
    if not isinstance(ctx.obj, dict):
        raise click.ClickException("Daemon config is unavailable in CLI context")
    config = ctx.obj.get("config")
    if not isinstance(config, DaemonConfig):
        raise click.ClickException("Daemon config is unavailable in CLI context")
    return get_daemon_client()
