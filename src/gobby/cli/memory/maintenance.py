from __future__ import annotations

import asyncio
import importlib
from collections.abc import Awaitable
from types import ModuleType
from typing import Any, Protocol

import click

from ._formatting import truncate


class _MemoryDeleteManager(Protocol):
    def delete_memory(self, memory_id: str) -> Awaitable[bool]: ...


class _MemoryListManager(_MemoryDeleteManager, Protocol):
    def list_memories(self, **kwargs: Any) -> list[Any]: ...


def _facade() -> ModuleType:
    return importlib.import_module("gobby.cli.memory")


async def _delete_memories(manager: _MemoryDeleteManager, memory_ids: list[str]) -> int:
    deleted = 0
    for memory_id in memory_ids:
        if await manager.delete_memory(memory_id):
            deleted += 1
    return deleted


def _list_all_memories(manager: _MemoryListManager, *, page_size: int = 1000) -> list[Any]:
    memories: list[Any] = []
    offset = 0
    while True:
        page = manager.list_memories(limit=page_size, offset=offset)
        memories.extend(page)
        if len(page) < page_size:
            return memories
        offset += page_size


@click.command("dedupe")
@click.option("--dry-run", is_flag=True, help="Show duplicates without deleting")
@click.pass_context
def dedupe_memories(ctx: click.Context, dry_run: bool) -> None:
    """Remove duplicate memories (same content, different IDs).

    Identifies memories with identical content but different IDs (caused by
    project_id variations) and removes duplicates, keeping the earliest one.

    Examples:

        gobby memory dedupe --dry-run   # Preview duplicates

        gobby memory dedupe             # Remove duplicates
    """
    memory_module = _facade()
    manager = memory_module.get_memory_manager(ctx)

    memories = _list_all_memories(manager)

    if not memories:
        click.echo("No memories found.")
        return

    content_groups: dict[str, list[tuple[str, str, str | None]]] = {}
    for memory in memories:
        normalized = memory.content.strip()
        if normalized not in content_groups:
            content_groups[normalized] = []
        content_groups[normalized].append((memory.id, memory.created_at, memory.project_id))

    duplicates_to_delete: list[str] = []
    duplicate_count = 0

    for content, entries in content_groups.items():
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
    from gobby.storage.hub.runtime import runtime_hub_database

    with runtime_hub_database(apply_migrations=False) as db:
        rows = db.fetchall(
            """
            SELECT id, content, source_session_id
            FROM memories
            WHERE project_id IS NULL AND source_type IN ('session', 'agent') AND source_session_id IS NOT NULL
            """,
            (),
        )

        if not rows:
            click.echo("No memories with NULL project_id from sessions found.")
            return

        click.echo(f"Found {len(rows)} memories with NULL project_id from sessions/agents.")

        session_ids = {row["source_session_id"] for row in rows if row["source_session_id"]}
        placeholders = ",".join("?" for _ in session_ids)
        session_project_ids: dict[str, str] = {}
        if session_ids:
            session_rows = db.fetchall(
                f"SELECT id, project_id FROM sessions WHERE id IN ({placeholders})",
                tuple(session_ids),
            )
            session_project_ids = {
                row["id"]: row["project_id"] for row in session_rows if row["project_id"]
            }

        updates: list[tuple[str, str]] = []
        fixable_count = 0
        for row in rows:
            memory_id = row["id"]
            session_id = row["source_session_id"]
            content_preview = row["content"][:50] if row["content"] else ""

            project_id = session_project_ids.get(session_id)
            if project_id:
                fixable_count += 1
                if dry_run:
                    click.echo(f"  Would fix {memory_id[:12]}: set project_id={project_id[:12]}")
                    click.echo(f"    Content: {content_preview}...")
                else:
                    updates.append((project_id, memory_id))
            elif dry_run:
                click.echo(
                    f"  Cannot fix {memory_id[:12]}: "
                    f"session {session_id} not found or has no project_id"
                )

        if dry_run:
            click.echo(f"\nWould fix {fixable_count} memories. Run without --dry-run to apply.")
        else:
            with db.transaction() as conn:
                for project_id, memory_id in updates:
                    conn.execute(
                        "UPDATE memories SET project_id = ? WHERE id = ?",
                        (project_id, memory_id),
                    )
            click.echo(f"Fixed {len(updates)} memories with project_id from their source sessions.")
