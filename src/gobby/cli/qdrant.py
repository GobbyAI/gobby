"""
CLI commands for Qdrant vector database management.
"""

import asyncio
import logging
import sys

import click

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
        from gobby.config.app import load_config

        config = load_config()
        url = config.databases.qdrant.url
    except (ImportError, FileNotFoundError, ValueError, AttributeError) as e:
        logger.debug(f"Could not load config for qdrant status: {e}")
        url = None

    status = asyncio.run(get_qdrant_status(qdrant_url=url))

    click.echo(f"Installed: {'yes' if status['installed'] else 'no'}")
    click.echo(f"Healthy:   {'yes' if status['healthy'] else 'no'}")
    if status["url"]:
        click.echo(f"URL:       {status['url']}")
