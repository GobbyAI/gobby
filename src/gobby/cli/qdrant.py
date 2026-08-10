"""
CLI commands for Qdrant vector database management.
"""

import asyncio
import logging
import sys

import click

from gobby.cli.runtime import get_cli_runtime

logger = logging.getLogger(__name__)


@click.group("qdrant")
def qdrant() -> None:
    """Manage Qdrant vector database service."""


@qdrant.command("install")
@click.option("--port", default=6333, help="HTTP port for Qdrant server")
def qdrant_install(port: int) -> None:
    """Install or reinstall Qdrant via Docker Compose."""
    from .installers.qdrant import install_qdrant

    result = install_qdrant(port=port)
    if result["success"]:
        click.echo("Qdrant installed successfully")
        click.echo(f"  URL: {result['qdrant_url']}")
        click.echo("\nRestart the daemon to apply: gobby restart")
    else:
        click.echo(f"Failed: {result['error']}", err=True)
        sys.exit(1)


@qdrant.command("status")
def qdrant_status() -> None:
    """Check Qdrant service status."""
    from .services import get_qdrant_status

    try:
        config = get_cli_runtime().require_config()
        url = config.databases.qdrant.url
    except Exception as e:
        logger.debug("Could not load config for qdrant status: %s", e)
        url = None

    status = asyncio.run(get_qdrant_status(qdrant_url=url))

    click.echo(f"Installed: {'yes' if status['installed'] else 'no'}")
    click.echo(f"Healthy:   {'yes' if status['healthy'] else 'no'}")
    if status["url"]:
        click.echo(f"URL:       {status['url']}")
