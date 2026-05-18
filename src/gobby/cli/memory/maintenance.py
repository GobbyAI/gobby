from __future__ import annotations

import asyncio
import importlib
from types import ModuleType

import click

from ._formatting import truncate


def _facade() -> ModuleType:
    return importlib.import_module("gobby.cli.memory")


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

    memories = manager.list_memories(limit=10000)

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
        deleted = 0
        for memory_id in duplicates_to_delete:
            if asyncio.run(manager.delete_memory(memory_id)):
                deleted += 1

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
    from gobby.storage.sessions import SessionManager

    memory_module = _facade()
    db = memory_module.LocalDatabase()
    session_mgr = SessionManager(db)

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

    fixed = 0
    for row in rows:
        memory_id = row["id"]
        session_id = row["source_session_id"]
        content_preview = row["content"][:50] if row["content"] else ""

        session = session_mgr.get(session_id)
        if session and session.project_id:
            if dry_run:
                click.echo(
                    f"  Would fix {memory_id[:12]}: set project_id={session.project_id[:12]}"
                )
                click.echo(f"    Content: {content_preview}...")
            else:
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE memories SET project_id = ? WHERE id = ?",
                        (session.project_id, memory_id),
                    )
                fixed += 1
        elif dry_run:
            click.echo(
                f"  Cannot fix {memory_id[:12]}: session {session_id} not found or has no project_id"
            )

    if dry_run:
        click.echo(f"\nWould fix {fixed} memories. Run without --dry-run to apply.")
    else:
        click.echo(f"Fixed {fixed} memories with project_id from their source sessions.")
