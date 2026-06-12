from __future__ import annotations

import asyncio
import importlib
from collections.abc import Awaitable, Coroutine
from types import ModuleType
from typing import Any, Protocol

import click

from ._formatting import truncate


class _MemoryDeleteManager(Protocol):
    def delete_memory(self, memory_id: str) -> Awaitable[bool]: ...


class _MemoryListManager(_MemoryDeleteManager, Protocol):
    def list_memories(self, **kwargs: Any) -> list[Any]: ...


class _MemoryProjectRepairManager(_MemoryListManager, Protocol):
    def fix_null_project_ids_from_sessions(
        self, *, dry_run: bool = False
    ) -> Coroutine[Any, Any, Any]: ...


def _facade() -> ModuleType:
    return importlib.import_module("gobby.cli.memory")


async def _delete_memories(manager: _MemoryDeleteManager, memory_ids: list[str]) -> int:
    deleted = 0
    for memory_id in memory_ids:
        if await manager.delete_memory(memory_id):
            deleted += 1
    return deleted


def _list_all_memories(
    manager: _MemoryListManager,
    *,
    page_size: int = 1000,
    max_results: int | None = None,
) -> list[Any]:
    memories: list[Any] = []
    offset = 0
    while True:
        remaining = None if max_results is None else max_results - len(memories)
        if remaining is not None and remaining <= 0:
            return memories
        limit = page_size if remaining is None else min(page_size, remaining)
        try:
            page = manager.list_memories(limit=limit, offset=offset)
        except (OSError, RuntimeError, ValueError) as exc:
            click.echo(f"Failed to list memories at offset {offset}: {exc}", err=True)
            return memories
        memories.extend(page)
        if len(page) < limit:
            return memories
        offset += limit


@click.command("dedupe")
@click.option("--dry-run", is_flag=True, help="Show duplicates without deleting")
@click.option("--yes", "-y", is_flag=True, help="Delete duplicates without confirmation")
@click.pass_context
def dedupe_memories(ctx: click.Context, dry_run: bool, yes: bool) -> None:
    """Remove duplicate memories (same content, different IDs).

    Identifies memories with identical content in the same project but different
    IDs and removes duplicates, keeping the earliest one.

    Examples:

        gobby memory dedupe --dry-run   # Preview duplicates

        gobby memory dedupe --yes       # Remove duplicates
    """
    memory_module = _facade()
    manager = memory_module.get_memory_manager(ctx)

    memories = _list_all_memories(manager)

    if not memories:
        click.echo("No memories found.")
        return

    content_groups: dict[tuple[str | None, str], list[tuple[str, str, str | None]]] = {}
    for memory in memories:
        normalized = memory.content.strip()
        key = (memory.project_id, normalized)
        if key not in content_groups:
            content_groups[key] = []
        content_groups[key].append((memory.id, memory.created_at, memory.project_id))

    duplicates_to_delete: list[str] = []
    duplicate_count = 0

    for (_project_id, content), entries in content_groups.items():
        if len(entries) > 1:
            duplicate_count += len(entries) - 1
            entries.sort(key=lambda x: x[1])
            keeper = entries[0]
            to_delete = entries[1:]

            if dry_run:
                click.echo(f"\nDuplicate content ({len(entries)} copies):")
                click.echo(f"  Content: {truncate(content, 80)}")
                click.echo(f"  Keep: {keeper[0][:12]} (created: {keeper[1][:19]})")
                for duplicate in to_delete:
                    click.echo(
                        f"  Delete: {duplicate[0][:12]} "
                        f"(created: {duplicate[1][:19]}, project: {duplicate[2]})"
                    )
            else:
                for duplicate in to_delete:
                    duplicates_to_delete.append(duplicate[0])

    if dry_run:
        click.echo(f"\nFound {duplicate_count} duplicate memories.")
        click.echo("Run without --dry-run to delete them.")
    else:
        if duplicates_to_delete and not yes:
            click.confirm(
                f"Delete {len(duplicates_to_delete)} duplicate memories? This cannot be undone.",
                abort=True,
            )
        deleted = asyncio.run(_delete_memories(manager, duplicates_to_delete))

        click.echo(f"Deleted {deleted} duplicate memories.")


@click.command("fix-null-project")
@click.option("--dry-run", is_flag=True, help="Show affected memories without updating")
@click.pass_context
def fix_null_project(ctx: click.Context, dry_run: bool) -> None:
    """Fix memories with NULL project_id from their source session.

    Finds memories with source_type='session' and NULL project_id, then
    looks up the source session to get the correct project_id.

    Examples:

        gobby memory fix-null-project --dry-run   # Preview changes

        gobby memory fix-null-project             # Apply fixes
    """
    memory_module = _facade()
    manager: _MemoryProjectRepairManager = memory_module.get_memory_manager(ctx)
    result: Any = asyncio.run(manager.fix_null_project_ids_from_sessions(dry_run=dry_run))

    if result.total == 0:
        click.echo("No memories with NULL project_id from sessions found.")
        return

    click.echo(f"Found {result.total} memories with NULL project_id from sessions/agents.")

    if dry_run:
        for repair in result.repairs:
            content_preview = repair.content[:50] if repair.content else ""
            if repair.project_id:
                click.echo(
                    f"  Would fix {repair.memory_id[:12]}: set project_id={repair.project_id[:12]}"
                )
                click.echo(f"    Content: {content_preview}...")
            else:
                click.echo(
                    f"  Cannot fix {repair.memory_id[:12]}: "
                    f"session {repair.source_session_id} not found or has no project_id"
                )

        click.echo(f"\nWould fix {result.fixable} memories. Run without --dry-run to apply.")
    else:
        click.echo(f"Fixed {result.fixed} memories with project_id from their source sessions.")
