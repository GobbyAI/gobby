"""
Gobby CLI entry point.
"""

import click
from gobby.config.app import load_config

from .daemon import restart, start, status, stop
from .init import init
from .install import install, uninstall
from .mcp import mcp_server
from .tasks import tasks


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


# Register commands
cli.add_command(start)
cli.add_command(stop)
cli.add_command(restart)
cli.add_command(status)
cli.add_command(mcp_server)
cli.add_command(init)
cli.add_command(install)
cli.add_command(uninstall)
cli.add_command(tasks)
