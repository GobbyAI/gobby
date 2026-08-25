"""
Gobby CLI entry point.
"""

import click

from gobby.config.bootstrap import load_bootstrap
from gobby.utils.version import get_version

from .agents import agents
from .auth import auth
from .build import build_command
from .clones import clones
from .communications import comms
from .cron import cron
from .cutover import cutover
from .daemon import health, restart, start, status, stop
from .daemon_lease import lease
from .datastores import datastores as datastores_cli
from .embeddings import embeddings
from .extensions import hooks, webhooks
from .files import files
from .github import github
from .hub_backup.cli import hub_backup
from .hub_maintenance import hub_maintenance
from .init import init
from .install import install
from .linear import linear
from .mcp import mcp_server
from .mcp_proxy import mcp_proxy
from .memory import memory
from .merge import merge
from .observations import observations
from .pack import pack, unpack
from .pipelines import pipelines
from .plan import plan
from .plans import plans
from .postgres import postgres_cli
from .profiles import profiles
from .projects import projects
from .qdrant import qdrant
from .rules import rules
from .runtime import CliRuntime
from .schema import schema
from .secrets import secrets
from .service import service
from .sessions import sessions
from .skills import skills
from .stages import stages
from .sync import sync
from .tasks import tasks
from .test_quality import test_quality
from .test_types import test_types
from .tokens import tokens
from .ui import ui
from .uninstall import uninstall
from .utils import get_gobby_home
from .variables import variables
from .worktrees import worktrees


def _version_callback(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    click.echo(f"gobby, version {get_version()}")
    ctx.exit()


@click.group()
@click.option(
    "--config",
    type=click.Path(exists=True),
    help="Path to custom configuration file",
)
@click.option(
    "--version",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=_version_callback,
    help="Show the version and exit.",
)
@click.pass_context
def cli(ctx: click.Context, config: str | None) -> None:
    """Gobby - Local-first daemon for AI coding assistants."""
    if ctx.invoked_subcommand == "start":
        load_bootstrap(str(get_gobby_home() / "bootstrap.yaml"))
    runtime = CliRuntime(config_file=config)
    ctx.obj = runtime
    ctx.call_on_close(runtime.close)


# Register commands
cli.add_command(start)
cli.add_command(stop)
cli.add_command(restart)
cli.add_command(status)
cli.add_command(health)
cli.add_command(lease)
cli.add_command(datastores_cli)
cli.add_command(embeddings)
cli.add_command(mcp_server)
cli.add_command(init)
cli.add_command(install)
cli.add_command(uninstall)
cli.add_command(tasks)
cli.add_command(test_quality)
cli.add_command(test_types)
cli.add_command(tokens)
cli.add_command(memory)
cli.add_command(observations)
cli.add_command(sessions)
cli.add_command(skills)
cli.add_command(stages)
cli.add_command(agents)
cli.add_command(worktrees)
cli.add_command(mcp_proxy)
cli.add_command(projects)
cli.add_command(profiles)
cli.add_command(rules)
cli.add_command(variables)
cli.add_command(merge)
cli.add_command(pipelines)
cli.add_command(github)
cli.add_command(linear)
cli.add_command(clones)
cli.add_command(cron)
cli.add_command(cutover)
cli.add_command(hooks)
cli.add_command(webhooks)
cli.add_command(ui)
cli.add_command(sync)
cli.add_command(auth)
cli.add_command(secrets)
cli.add_command(service)
cli.add_command(qdrant)
cli.add_command(postgres_cli)
cli.add_command(pack)
cli.add_command(unpack)
cli.add_command(files)
cli.add_command(hub_backup)
cli.add_command(hub_maintenance)
cli.add_command(comms)
cli.add_command(build_command)
cli.add_command(plan)
cli.add_command(plans)
cli.add_command(schema)
