from __future__ import annotations

from typing import TYPE_CHECKING

import click

from gobby.cli.utils import resolve_project_ref
from gobby.config.app import DaemonConfig
from gobby.memory.manager import MemoryManager
from gobby.storage.hub.runtime import open_runtime_hub_database

from .main import memory

if TYPE_CHECKING:
    from gobby.utils.daemon_client import DaemonClient


def get_memory_manager(ctx: click.Context) -> MemoryManager:
    """Get memory manager."""
    config: DaemonConfig = ctx.obj["config"]
    db = open_runtime_hub_database(apply_migrations=False)

    return MemoryManager(db, config.memory)


def _get_daemon_client(ctx: click.Context) -> DaemonClient:
    """Get a DaemonClient for calling daemon HTTP API."""
    from gobby.utils.daemon_client import DaemonClient

    config: DaemonConfig = ctx.obj["config"]
    return DaemonClient(host="localhost", port=config.daemon_port)


def resolve_memory_id(
    manager: MemoryManager, memory_ref: str, project_id: str | None = None
) -> str:
    """Resolve memory reference (UUID or prefix) to full ID.

    Args:
        manager: MemoryManager instance
        memory_ref: UUID or prefix to resolve
        project_id: If provided, scope lookup to this project
    """
    # Try exact match first
    # Optimization: check 36 chars?
    if len(memory_ref) == 36 and manager.get_memory(memory_ref, project_id=project_id):
        return memory_ref

    # Try prefix match using MemoryManager method
    memories = manager.find_by_prefix(memory_ref, limit=5, project_id=project_id)

    if not memories:
        raise click.ClickException(f"Memory not found: {memory_ref}")

    if len(memories) > 1:
        click.echo(f"Ambiguous memory reference '{memory_ref}' matches:", err=True)
        for mem in memories:
            click.echo(f"  {mem.id}", err=True)
        raise click.ClickException(f"Ambiguous memory reference: {memory_ref}")

    return memories[0].id


__all__ = [
    "MemoryManager",
    "get_memory_manager",
    "memory",
    "resolve_memory_id",
    "resolve_project_ref",
]
