"""
Gobby CLI entry point.
"""

import click

from gobby.config.app import load_config

from .agents import agents
from .auth import auth
from .clones import clones
from .code_index import code_index
from .compress import compress
from .cron import cron
from .daemon import restart, start, status, stop
from .export_import import export_cmd, import_cmd
from .extensions import hooks, webhooks
from .github import github
from .init import init
from .install import install, uninstall
from .linear import linear
from .mcp import mcp_server
from .mcp_proxy import mcp_proxy
from .memory import memory
from .merge import merge
from .pipelines import pipelines
from .projects import projects
from .rules import rules
from .sessions import sessions
from .setup import setup
from .skills import skills
from .sync import sync
from .tasks import tasks
from .ui import ui
from .workflows import workflows
from .worktrees import worktrees


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
cli.add_command(setup)
cli.add_command(install)
cli.add_command(uninstall)
cli.add_command(tasks)
cli.add_command(memory)
cli.add_command(sessions)
cli.add_command(skills)
cli.add_command(agents)
cli.add_command(worktrees)
cli.add_command(mcp_proxy)
cli.add_command(projects)
cli.add_command(rules)
cli.add_command(workflows)
cli.add_command(merge)
cli.add_command(pipelines)
cli.add_command(github)
cli.add_command(linear)
cli.add_command(clones)
cli.add_command(cron)
cli.add_command(hooks)
cli.add_command(webhooks)
cli.add_command(ui)
cli.add_command(sync)
cli.add_command(auth)
cli.add_command(compress)
cli.add_command(export_cmd)
cli.add_command(import_cmd)
cli.add_command(code_index)
