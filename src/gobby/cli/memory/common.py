from __future__ import annotations

from typing import TYPE_CHECKING

import click

from gobby.config.app import DaemonConfig

if TYPE_CHECKING:
    from gobby.utils.daemon_client import DaemonClient


def _get_daemon_client(ctx: click.Context) -> DaemonClient:
    """Get a DaemonClient for calling daemon HTTP API."""
    from gobby.utils.daemon_client import DaemonClient

    config: DaemonConfig = ctx.obj["config"]
    return DaemonClient(host="localhost", port=config.daemon_port)
