"""Typed process-lifetime resources for Click commands."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

import click

from gobby.config.app import DaemonConfig
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.hub.runtime import runtime_hub_database
from gobby.storage.projects import LocalProjectManager
from gobby.utils.project_context import get_project_context


def _load_runtime_config(config_file: str | None, database: HubDatabase) -> DaemonConfig:
    from gobby.cli.utils import load_full_config_from_db

    return load_full_config_from_db(config_file, database=database)


@dataclass(init=False)
class CliRuntime:
    """Own resources shared by one top-level CLI invocation."""

    config_file: str | None
    config_loader: Callable[[str | None, HubDatabase], DaemonConfig] = _load_runtime_config
    exit_stack: ExitStack = field(default_factory=ExitStack)
    _config: DaemonConfig | None = field(default=None, init=False, repr=False)
    _database: HubDatabase | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __init__(
        self,
        config_file: str | None,
        config: DaemonConfig | None = None,
        config_loader: Callable[[str | None, HubDatabase], DaemonConfig] = _load_runtime_config,
        exit_stack: ExitStack | None = None,
    ) -> None:
        self.config_file = config_file
        self.config_loader = config_loader
        self.exit_stack = exit_stack if exit_stack is not None else ExitStack()
        self._config = config
        self._database = None
        self._closed = False

    @property
    def config(self) -> DaemonConfig:
        """Load DB-backed configuration only when a command needs it."""
        return self.require_config()

    @config.setter
    def config(self, value: DaemonConfig) -> None:
        self._config = value

    def require_config(self, *, apply_migrations: bool = True) -> DaemonConfig:
        """Return configuration, controlling migrations on its first database open."""
        if self._config is None:
            database = self.require_database(apply_migrations=apply_migrations)
            self._config = self.config_loader(self.config_file, database)
        return self._config

    def require_database(self, *, apply_migrations: bool = True) -> HubDatabase:
        """Return the invocation's database, opening it on first use."""
        if self._closed:
            raise RuntimeError("CLI runtime is already closed")
        if self._database is None:
            self._database = self.exit_stack.enter_context(
                runtime_hub_database(self.config_file, apply_migrations=apply_migrations)
            )
        return self._database

    def close(self) -> None:
        """Close every owned resource exactly once."""
        if self._closed:
            return
        self._closed = True
        self.exit_stack.close()
        self._database = None


def get_cli_runtime(ctx: click.Context | None = None) -> CliRuntime:
    """Return the typed runtime attached to the root Click context."""
    resolved_ctx = ctx or click.get_current_context(silent=True)
    if resolved_ctx is None:
        raise RuntimeError("CLI runtime is unavailable outside a Click invocation")
    runtime = resolved_ctx.find_root().obj
    if not isinstance(runtime, CliRuntime):
        raise RuntimeError("Click context does not contain a CliRuntime")
    return runtime


def require_cli_database(ctx: click.Context | None = None) -> HubDatabase:
    """Borrow the shared database for the current CLI invocation."""
    return get_cli_runtime(ctx).require_database()


def resolve_cli_project(
    project_manager: LocalProjectManager,
    project_ref: str | None = None,
    *,
    require_project: bool = True,
) -> str:
    """Resolve an explicit project ref or the current project for CLI commands."""
    if project_ref:
        project = project_manager.resolve_ref(project_ref)
        if not project or project.deleted_at:
            raise click.ClickException(f"Project not found: {project_ref}")
        return project.id

    if not require_project:
        return ""

    context = get_project_context(cwd=Path.cwd())
    if not context or not context.get("id"):
        raise click.ClickException("Not in a gobby project directory. Run 'gobby init' first.")
    return str(context["id"])
