"""
Gobby CLI entry point.
"""

import importlib
from collections.abc import Iterator, MutableMapping

import click

from gobby.config.bootstrap import load_bootstrap
from gobby.utils.version import get_version

from .runtime import CliRuntime
from .utils import get_gobby_home

# Command name -> (module under gobby.cli, attribute). Each module is imported only
# when its command runs or is listed, so `gobby mcp-server` (every agent's stdio
# bridge) does not pay every command group's import cost before answering initialize.
_COMMAND_SOURCES: dict[str, tuple[str, str]] = {
    "start": ("daemon_start", "start"),
    "stop": ("daemon", "stop"),
    "restart": ("daemon", "restart"),
    "status": ("daemon", "status"),
    "health": ("daemon_health", "health"),
    "lease": ("daemon_lease", "lease"),
    "datastores": ("datastores", "datastores"),
    "embeddings": ("embeddings", "embeddings"),
    "mcp-server": ("mcp", "mcp_server"),
    "init": ("init", "init"),
    "install": ("install", "install"),
    "uninstall": ("uninstall", "uninstall"),
    "tasks": ("tasks", "tasks"),
    "test-quality": ("test_quality", "test_quality"),
    "test-types": ("test_types", "test_types"),
    "tokens": ("tokens", "tokens"),
    "memory": ("memory", "memory"),
    "feedback": ("feedback", "feedback"),
    "observations": ("observations", "observations"),
    "sessions": ("sessions", "sessions"),
    "skills": ("skills", "skills"),
    "stages": ("stages", "stages"),
    "agents": ("agents", "agents"),
    "worktrees": ("worktrees", "worktrees"),
    "workspaces": ("workspaces", "workspaces"),
    "panes": ("workspaces", "panes"),
    "nodes": ("workspaces", "nodes"),
    "mcp-proxy": ("mcp_proxy", "mcp_proxy"),
    "projects": ("projects", "projects"),
    "profiles": ("profiles", "profiles"),
    "rules": ("rules", "rules"),
    "variables": ("variables", "variables"),
    "merge": ("merge", "merge"),
    "pipelines": ("pipelines", "pipelines"),
    "clones": ("clones", "clones"),
    "cron": ("cron", "cron"),
    "cutover": ("cutover", "cutover"),
    "hooks": ("extensions", "hooks"),
    "webhooks": ("extensions", "webhooks"),
    "ui": ("ui", "ui"),
    "sync": ("sync", "sync"),
    "auth": ("auth", "auth"),
    "secrets": ("secrets", "secrets"),
    "service": ("service", "service"),
    "qdrant": ("qdrant", "qdrant"),
    "postgres": ("postgres", "postgres_cli"),
    "pack": ("pack", "pack"),
    "unpack": ("pack", "unpack"),
    "files": ("files", "files"),
    "hub-backup": ("hub_backup.cli", "hub_backup"),
    "hub-maintenance": ("hub_maintenance", "hub_maintenance"),
    "comms": ("communications", "comms"),
    "build": ("build", "build_command"),
    "plan": ("plan", "plan"),
    "plans": ("plans", "plans"),
    "schema": ("schema", "schema"),
}


class _LazyCommands(MutableMapping[str, click.Command]):
    """Click's command table, importing each command's module on first lookup."""

    def __init__(self, sources: dict[str, tuple[str, str]]) -> None:
        self._sources = dict(sources)
        self._loaded: dict[str, click.Command] = {}

    def __getitem__(self, name: str) -> click.Command:
        if name not in self._loaded:
            module_name, attribute = self._sources[name]
            module = importlib.import_module(f"{__name__}.{module_name}")
            self._loaded[name] = getattr(module, attribute)
        return self._loaded[name]

    def __setitem__(self, name: str, command: click.Command) -> None:
        self._sources.pop(name, None)
        self._loaded[name] = command

    def __delitem__(self, name: str) -> None:
        if name not in self:
            raise KeyError(name)
        self._sources.pop(name, None)
        self._loaded.pop(name, None)

    def __contains__(self, name: object) -> bool:
        return name in self._sources or name in self._loaded

    def __iter__(self) -> Iterator[str]:
        yield from self._sources
        yield from (name for name in self._loaded if name not in self._sources)

    def __len__(self) -> int:
        return len(self._sources.keys() | self._loaded.keys())


def _version_callback(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    click.echo(f"gobby, version {get_version()}")
    ctx.exit()


@click.group(commands=_LazyCommands(_COMMAND_SOURCES))
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
    """Gobby - fleet management for AI coding agents."""
    if ctx.invoked_subcommand == "start":
        load_bootstrap(str(get_gobby_home() / "bootstrap.yaml"))
    runtime = CliRuntime(config_file=config)
    ctx.obj = runtime
    ctx.call_on_close(runtime.close)
