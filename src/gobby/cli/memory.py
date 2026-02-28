from __future__ import annotations

import asyncio
import urllib.parse
from typing import TYPE_CHECKING

import click

from gobby.cli.utils import resolve_project_ref
from gobby.config.app import DaemonConfig
from gobby.memory.manager import MemoryManager
from gobby.storage.database import LocalDatabase

if TYPE_CHECKING:
    from gobby.utils.daemon_client import DaemonClient


def get_memory_manager(ctx: click.Context) -> MemoryManager:
    """Get memory manager."""
    config: DaemonConfig = ctx.obj["config"]
    db = LocalDatabase()

    return MemoryManager(db, config.memory)


@click.group()
def memory() -> None:
    """Manage Gobby memories."""
    pass


@memory.command()
@click.argument("content")
@click.option(
    "--type", "-t", "memory_type", default="fact", help="Type of memory (fact, preference, etc.)"
)
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.pass_context
def create(ctx: click.Context, content: str, memory_type: str, project_ref: str | None) -> None:
    """Create a new memory."""
    project_id = resolve_project_ref(project_ref) if project_ref else None
    manager = get_memory_manager(ctx)
    memory = asyncio.run(
        manager.create_memory(
            content=content,
            memory_type=memory_type,
            project_id=project_id,
            source_type="cli",
        )
    )
    click.echo(f"Created memory: {memory.id} - {memory.content}")


@memory.command()
@click.argument("query", required=False)
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.option("--limit", "-n", default=10, help="Max results")
@click.option("--tags-all", "tags_all", help="Require ALL tags (comma-separated)")
@click.option("--tags-any", "tags_any", help="Require ANY tag (comma-separated)")
@click.option("--tags-none", "tags_none", help="Exclude memories with these tags (comma-separated)")
@click.pass_context
def recall(
    ctx: click.Context,
    query: str | None,
    project_ref: str | None,
    limit: int,
    tags_all: str | None,
    tags_any: str | None,
    tags_none: str | None,
) -> None:
    """Retrieve memories with optional tag filtering."""
    project_id = resolve_project_ref(project_ref) if project_ref else None
    manager = get_memory_manager(ctx)

    # Parse comma-separated tags
    tags_all_list = [t.strip() for t in tags_all.split(",") if t.strip()] if tags_all else None
    tags_any_list = [t.strip() for t in tags_any.split(",") if t.strip()] if tags_any else None
    tags_none_list = [t.strip() for t in tags_none.split(",") if t.strip()] if tags_none else None

    memories = asyncio.run(
        manager.search_memories(
            query=query,
            project_id=project_id,
            limit=limit,
            tags_all=tags_all_list,
            tags_any=tags_any_list,
            tags_none=tags_none_list,
        )
    )
    if not memories:
        click.echo("No memories found.")
        return

    for mem in memories:
        tags_str = f" [{', '.join(mem.tags)}]" if mem.tags else ""
        click.echo(f"[{mem.id[:8]}] ({mem.memory_type}){tags_str} {mem.content}")


@memory.command()
@click.argument("memory_ref")
@click.pass_context
def delete(ctx: click.Context, memory_ref: str) -> None:
    """Delete a memory by ID (UUID or prefix)."""
    manager = get_memory_manager(ctx)
    memory_id = resolve_memory_id(manager, memory_ref)
    success = asyncio.run(manager.delete_memory(memory_id))
    if success:
        click.echo(f"Deleted memory: {memory_id}")
    else:
        click.echo(f"Memory not found: {memory_id}")


@memory.command("list")
@click.option("--type", "-t", "memory_type", help="Filter by memory type")
@click.option("--limit", "-n", default=50, help="Max results")
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.option("--tags-all", "tags_all", help="Require ALL tags (comma-separated)")
@click.option("--tags-any", "tags_any", help="Require ANY tag (comma-separated)")
@click.option("--tags-none", "tags_none", help="Exclude memories with these tags (comma-separated)")
@click.pass_context
def list_memories(
    ctx: click.Context,
    memory_type: str | None,
    project_ref: str | None,
    limit: int,
    tags_all: str | None,
    tags_any: str | None,
    tags_none: str | None,
) -> None:
    """List all memories with optional filtering."""
    project_id = resolve_project_ref(project_ref) if project_ref else None
    manager = get_memory_manager(ctx)

    # Parse comma-separated tags
    tags_all_list = [t.strip() for t in tags_all.split(",") if t.strip()] if tags_all else None
    tags_any_list = [t.strip() for t in tags_any.split(",") if t.strip()] if tags_any else None
    tags_none_list = [t.strip() for t in tags_none.split(",") if t.strip()] if tags_none else None

    memories = manager.list_memories(
        project_id=project_id,
        memory_type=memory_type,
        limit=limit,
        tags_all=tags_all_list,
        tags_any=tags_any_list,
        tags_none=tags_none_list,
    )
    if not memories:
        click.echo("No memories found.")
        return

    for mem in memories:
        tags_str = f" [{', '.join(mem.tags)}]" if mem.tags else ""
        click.echo(f"[{mem.id[:8]}] ({mem.memory_type}){tags_str}")
        click.echo(f"  {mem.content[:100]}{'...' if len(mem.content) > 100 else ''}")


@memory.command("show")
@click.argument("memory_ref")
@click.pass_context
def show_memory(ctx: click.Context, memory_ref: str) -> None:
    """Show details of a specific memory (UUID or prefix)."""
    manager = get_memory_manager(ctx)
    memory_id = resolve_memory_id(manager, memory_ref)
    memory = manager.get_memory(memory_id)
    if not memory:
        click.echo(f"Memory not found: {memory_id}")
        return

    click.echo(f"ID: {memory.id}")
    click.echo(f"Type: {memory.memory_type}")
    click.echo(f"Created: {memory.created_at}")
    click.echo(f"Updated: {memory.updated_at}")
    click.echo(f"Source: {memory.source_type}")
    click.echo(f"Access Count: {memory.access_count}")
    if memory.tags:
        click.echo(f"Tags: {', '.join(memory.tags)}")
    click.echo(f"Content:\n{memory.content}")


@memory.command("update")
@click.argument("memory_ref")
@click.option("--content", "-c", help="New content")
@click.option("--tags", "-t", help="New tags (comma-separated)")
@click.pass_context
def update_memory(
    ctx: click.Context,
    memory_ref: str,
    content: str | None,
    tags: str | None,
) -> None:
    """Update an existing memory (UUID or prefix)."""
    manager = get_memory_manager(ctx)
    memory_id = resolve_memory_id(manager, memory_ref)

    # Parse tags if provided
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    if tag_list is not None and len(tag_list) == 0:
        tag_list = None

    try:
        memory = asyncio.run(
            manager.update_memory(
                memory_id=memory_id,
                content=content,
                tags=tag_list,
            )
        )
        click.echo(f"Updated memory: {memory.id}")
        click.echo(f"  Content: {memory.content[:80]}{'...' if len(memory.content) > 80 else ''}")
    except ValueError as e:
        click.echo(f"Error: {e}")


@memory.command("stats")
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.pass_context
def memory_stats(ctx: click.Context, project_ref: str | None) -> None:
    """Show memory system statistics."""
    project_id = resolve_project_ref(project_ref) if project_ref else None
    manager = get_memory_manager(ctx)
    stats = manager.get_stats(project_id=project_id)

    click.echo("Memory Statistics:")
    click.echo(f"  Total Memories: {stats['total_count']}")
    if stats["by_type"]:
        click.echo("  By Type:")
        for mem_type, count in stats["by_type"].items():
            click.echo(f"    {mem_type}: {count}")


@memory.command("export")
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.option(
    "--output", "-o", "output_file", type=click.Path(), help="Output file (stdout if not specified)"
)
@click.option("--no-metadata", is_flag=True, help="Exclude memory metadata")
@click.option("--no-stats", is_flag=True, help="Exclude summary statistics")
@click.pass_context
def export_memories(
    ctx: click.Context,
    project_ref: str | None,
    output_file: str | None,
    no_metadata: bool,
    no_stats: bool,
) -> None:
    """Export memories as markdown.

    Exports all memories (or filtered by project) to a formatted markdown document.
    Output goes to stdout by default, or to a file with --output.

    Examples:

        gobby memory export                    # Export all to stdout

        gobby memory export -o memories.md    # Export to file

        gobby memory export -p myproject      # Export specific project

        gobby memory export --no-metadata     # Content only, no metadata
    """
    project_id = resolve_project_ref(project_ref) if project_ref else None
    manager = get_memory_manager(ctx)

    markdown = manager.export_markdown(
        project_id=project_id,
        include_metadata=not no_metadata,
        include_stats=not no_stats,
    )

    if output_file:
        from pathlib import Path

        path = Path(output_file)
        try:
            path.write_text(markdown, encoding="utf-8")
            click.echo(f"Exported memories to {output_file}")
        except OSError as e:
            raise click.ClickException(f"Failed to write to {output_file}: {e}") from e
    else:
        click.echo(markdown)


@memory.command("dedupe")
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
    manager = get_memory_manager(ctx)

    # Get all memories
    memories = manager.list_memories(limit=10000)

    if not memories:
        click.echo("No memories found.")
        return

    # Group by normalized content
    content_groups: dict[str, list[tuple[str, str, str | None]]] = {}
    for m in memories:
        normalized = m.content.strip()
        if normalized not in content_groups:
            content_groups[normalized] = []
        content_groups[normalized].append((m.id, m.created_at, m.project_id))

    # Find duplicates
    duplicates_to_delete: list[str] = []
    duplicate_count = 0

    for content, entries in content_groups.items():
        if len(entries) > 1:
            duplicate_count += len(entries) - 1
            # Sort by created_at to keep earliest
            entries.sort(key=lambda x: x[1])
            keeper = entries[0]
            to_delete = entries[1:]

            if dry_run:
                click.echo(f"\nDuplicate content ({len(entries)} copies):")
                click.echo(f"  Content: {content[:80]}{'...' if len(content) > 80 else ''}")
                click.echo(f"  Keep: {keeper[0][:12]} (created: {keeper[1][:19]})")
                for d in to_delete:
                    click.echo(f"  Delete: {d[0][:12]} (created: {d[1][:19]}, project: {d[2]})")
            else:
                for d in to_delete:
                    duplicates_to_delete.append(d[0])

    if dry_run:
        click.echo(f"\nFound {duplicate_count} duplicate memories.")
        click.echo("Run without --dry-run to delete them.")
    else:
        # Delete duplicates
        deleted = 0
        for memory_id in duplicates_to_delete:
            if asyncio.run(manager.delete_memory(memory_id)):
                deleted += 1

        click.echo(f"Deleted {deleted} duplicate memories.")


@memory.command("fix-null-project")
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
    from gobby.storage.sessions import LocalSessionManager

    db = LocalDatabase()
    session_mgr = LocalSessionManager(db)

    # Find memories with NULL project_id and session source
    rows = db.fetchall(
        """
        SELECT id, content, source_session_id
        FROM memories
        WHERE project_id IS NULL AND source_type = 'session' AND source_session_id IS NOT NULL
        """,
        (),
    )

    if not rows:
        click.echo("No memories with NULL project_id from sessions found.")
        return

    click.echo(f"Found {len(rows)} memories with NULL project_id from sessions.")

    fixed = 0
    for row in rows:
        memory_id = row["id"]
        session_id = row["source_session_id"]
        content_preview = row["content"][:50] if row["content"] else ""

        # Look up session to get project_id
        session = session_mgr.get(session_id)
        if session and session.project_id:
            if dry_run:
                click.echo(
                    f"  Would fix {memory_id[:12]}: set project_id={session.project_id[:12]}"
                )
                click.echo(f"    Content: {content_preview}...")
            else:
                # Update the memory's project_id
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE memories SET project_id = ? WHERE id = ?",
                        (session.project_id, memory_id),
                    )
                fixed += 1
        else:
            if dry_run:
                click.echo(
                    f"  Cannot fix {memory_id[:12]}: session {session_id} not found or has no project_id"
                )

    if dry_run:
        click.echo(f"\nWould fix {fixed} memories. Run without --dry-run to apply.")
    else:
        click.echo(f"Fixed {fixed} memories with project_id from their source sessions.")


@memory.command("backup")
@click.option(
    "--output",
    "-o",
    "output_path",
    type=click.Path(),
    help="Output file path (default: .gobby/memories.jsonl)",
)
@click.pass_context
def backup_memories(ctx: click.Context, output_path: str | None) -> None:
    """Backup memories to JSONL file.

    Exports all memories to a JSONL file for backup/disaster recovery.
    This runs synchronously and can be used even when the daemon is not running.

    Examples:

        gobby memory backup                           # Export to .gobby/memories.jsonl

        gobby memory backup -o ~/backups/mem.jsonl   # Export to custom path
    """
    from pathlib import Path

    from gobby.config.persistence import MemoryBackupConfig
    from gobby.sync.memories import MemoryBackupManager

    manager = get_memory_manager(ctx)

    # Create a backup manager with custom or default path
    if output_path:
        export_path = Path(output_path)
    else:
        export_path = Path(".gobby/memories.jsonl")

    config = MemoryBackupConfig(enabled=True, export_path=export_path)
    backup_mgr = MemoryBackupManager(
        db=manager.db,
        memory_manager=manager,
        config=config,
    )

    count = backup_mgr.backup_sync()
    if count > 0:
        click.echo(f"Backed up {count} memories to {export_path}")
    else:
        click.echo("No memories to backup.")


@memory.command("reindex-embeddings")
@click.pass_context
def reindex_embeddings(ctx: click.Context) -> None:
    """Regenerate embeddings for all memories.

    Generates embedding vectors for all stored memories using the configured
    embedding model. Useful after changing models or for initial setup.

    Examples:

        gobby memory reindex-embeddings
    """
    manager = get_memory_manager(ctx)
    click.echo("Reindexing memory embeddings...")
    result = asyncio.run(manager.reindex_embeddings())

    if result["success"]:
        total = result.get("total_memories", 0)
        generated = result.get("embeddings_generated", 0)
        click.echo(f"Reindexed {generated}/{total} memory embeddings.")
    else:
        click.echo(f"Error: {result.get('error', 'Unknown error')}")


def _get_daemon_client(ctx: click.Context) -> DaemonClient:
    """Get a DaemonClient for calling daemon HTTP API."""
    from gobby.utils.daemon_client import DaemonClient

    config: DaemonConfig = ctx.obj["config"]
    return DaemonClient(host="localhost", port=config.daemon_port)


@memory.command("rebuild-crossrefs")
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.pass_context
def rebuild_crossrefs(ctx: click.Context, project_ref: str | None) -> None:
    """Rebuild cross-references between memories (requires running daemon).

    Uses vector similarity to find related memories and create links.
    These links power the 2D memory graph visualization.

    Examples:

        gobby memory rebuild-crossrefs

        gobby memory rebuild-crossrefs -p myproject
    """
    client = _get_daemon_client(ctx)
    is_healthy, err = client.check_health()
    if not is_healthy:
        raise click.ClickException(f"Daemon not running: {err}")

    click.echo("Rebuilding cross-references (this may take a while)...")
    project_id = resolve_project_ref(project_ref, exit_on_not_found=True) if project_ref else None
    params = f"?project_id={urllib.parse.quote(str(project_id))}" if project_id else ""
    response = client.call_http_api(
        f"/api/memories/crossrefs/rebuild{params}", method="POST", timeout=600.0
    )
    if not response.ok:
        raise click.ClickException(f"Rebuild failed (HTTP {response.status_code}): {response.text}")
    try:
        data = response.json()
    except ValueError as e:
        raise click.ClickException(f"Invalid response from daemon: {e}") from e
    click.echo(
        f"Done: {data.get('memories_processed', '?')} memories processed, "
        f"{data.get('crossrefs_created', '?')} crossrefs created"
    )


@memory.command("rebuild-graph")
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.pass_context
def rebuild_graph(ctx: click.Context, project_ref: str | None) -> None:
    """Extract entities from memories into the knowledge graph (requires running daemon).

    Processes all memories through LLM entity extraction and stores
    results in Neo4j. Powers the 3D knowledge graph visualization.

    Examples:

        gobby memory rebuild-graph

        gobby memory rebuild-graph -p myproject
    """
    client = _get_daemon_client(ctx)
    is_healthy, err = client.check_health()
    if not is_healthy:
        raise click.ClickException(f"Daemon not running: {err}")

    click.echo("Rebuilding knowledge graph (this may take several minutes)...")
    project_id = resolve_project_ref(project_ref, exit_on_not_found=True) if project_ref else None
    params = f"?project_id={urllib.parse.quote(str(project_id))}" if project_id else ""
    response = client.call_http_api(
        f"/api/memories/graph/rebuild{params}", method="POST", timeout=600.0
    )
    if not response.ok:
        raise click.ClickException(f"Rebuild failed (HTTP {response.status_code}): {response.text}")
    try:
        data = response.json()
    except ValueError as e:
        raise click.ClickException(f"Invalid response from daemon: {e}") from e
    click.echo(
        f"Done: {data.get('memories_extracted', '?')}/{data.get('memories_processed', '?')} "
        f"memories extracted, {data.get('errors', '?')} errors"
    )


def resolve_memory_id(manager: MemoryManager, memory_ref: str) -> str:
    """Resolve memory reference (UUID or prefix) to full ID."""
    # Try exact match first
    # Optimization: check 36 chars?
    if len(memory_ref) == 36 and manager.get_memory(memory_ref):
        return memory_ref

    # Try prefix match using MemoryManager method
    memories = manager.find_by_prefix(memory_ref, limit=5)

    if not memories:
        raise click.ClickException(f"Memory not found: {memory_ref}")

    if len(memories) > 1:
        click.echo(f"Ambiguous memory reference '{memory_ref}' matches:", err=True)
        for mem in memories:
            click.echo(f"  {mem.id}", err=True)
        raise click.ClickException(f"Ambiguous memory reference: {memory_ref}")

    return memories[0].id
