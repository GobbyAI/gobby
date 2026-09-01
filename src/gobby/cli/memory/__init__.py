from __future__ import annotations

import click

from gobby.cli.runtime import get_cli_runtime, require_cli_database
from gobby.cli.utils import resolve_project_ref
from gobby.config.app import DaemonConfig
from gobby.memory.facade import AmbiguousMemoryReferenceError
from gobby.memory.manager import MemoryManager

from .common import _get_daemon_client
from .main import memory


def get_memory_manager(ctx: click.Context) -> MemoryManager:
    """Get memory manager."""
    config: DaemonConfig = get_cli_runtime(ctx).config
    db = require_cli_database(ctx)

    return MemoryManager(db, config.memory)


def resolve_memory_id(
    manager: MemoryManager, memory_ref: str, project_id: str | None = None
) -> str:
    """Resolve memory reference (UUID or prefix) to full ID.

    Args:
        manager: MemoryManager instance
        memory_ref: UUID or prefix to resolve
        project_id: If provided, scope lookup to this project
    """
    try:
        memory_id = manager.resolve_memory_id(memory_ref, project_id=project_id)
    except AmbiguousMemoryReferenceError as exc:
        click.echo(f"Ambiguous memory reference '{memory_ref}' matches:", err=True)
        for candidate in exc.candidates:
            click.echo(f"  {candidate}", err=True)
        raise click.ClickException(f"Ambiguous memory reference: {memory_ref}") from exc
    if memory_id is None:
        raise click.ClickException(f"Memory not found: {memory_ref}")
    return memory_id


__all__ = [
    "MemoryManager",
    "get_memory_manager",
    "memory",
    "resolve_memory_id",
    "resolve_project_ref",
]
