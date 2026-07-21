from __future__ import annotations

from typing import TYPE_CHECKING

import click

from gobby.cli.utils_config import get_daemon_client

if TYPE_CHECKING:
    from gobby.utils.daemon_client import DaemonClient


def _get_daemon_client(ctx: click.Context) -> DaemonClient:
    """Get a DaemonClient for calling daemon HTTP API."""
    from gobby.cli.runtime import get_cli_runtime

    get_cli_runtime(ctx)
    return get_daemon_client()
