"""Typed process-lifetime resources for Click commands."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field

import click

from gobby.config.app import DaemonConfig
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.hub.runtime import runtime_hub_database


@dataclass
class CliRuntime:
    """Own resources shared by one top-level CLI invocation."""

    config_file: str | None
    config: DaemonConfig = field(default_factory=DaemonConfig)
    exit_stack: ExitStack = field(default_factory=ExitStack)
    _database: HubDatabase | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def require_database(self) -> HubDatabase:
        """Return the invocation's database, opening it on first use."""
        if self._closed:
            raise RuntimeError("CLI runtime is already closed")
        if self._database is None:
            self._database = self.exit_stack.enter_context(runtime_hub_database(self.config_file))
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
