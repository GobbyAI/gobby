from __future__ import annotations

import asyncio
import time
import urllib.parse
from typing import TYPE_CHECKING

import click
import httpx

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
            source_type="user",
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
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.pass_context
def delete(ctx: click.Context, memory_ref: str, project_ref: str | None = None) -> None:
    """Delete a memory by ID (UUID or prefix)."""
    project_id = resolve_project_ref(project_ref) if project_ref else None
    manager = get_memory_manager(ctx)
    memory_id = resolve_memory_id(manager, memory_ref, project_id=project_id)
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
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.pass_context
def show_memory(ctx: click.Context, memory_ref: str, project_ref: str | None = None) -> None:
    """Show details of a specific memory (UUID or prefix)."""
    project_id = resolve_project_ref(project_ref) if project_ref else None
    manager = get_memory_manager(ctx)
    memory_id = resolve_memory_id(manager, memory_ref, project_id=project_id)
    memory = manager.get_memory(memory_id, project_id=project_id)
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
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.pass_context
def update_memory(
    ctx: click.Context,
    memory_ref: str,
    content: str | None,
    tags: str | None,
    project_ref: str | None = None,
) -> None:
    """Update an existing memory (UUID or prefix)."""
    project_id = resolve_project_ref(project_ref) if project_ref else None
    manager = get_memory_manager(ctx)
    memory_id = resolve_memory_id(manager, memory_ref, project_id=project_id)

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
    from gobby.storage.sessions import SessionManager

    db = LocalDatabase()
    session_mgr = SessionManager(db)

    # Find memories with NULL project_id and session source
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
@click.option("--quiet", "-q", is_flag=True, help="Suppress output")
@click.pass_context
def backup_memories(ctx: click.Context, output_path: str | None, quiet: bool) -> None:
    """Backup memories to JSONL file.

    Exports project-scoped memories to a JSONL file for backup/disaster recovery.
    This runs synchronously and can be used even when the daemon is not running.

    Examples:

        gobby memory backup                           # Export to .gobby/memories.jsonl

        gobby memory backup -o ~/backups/mem.jsonl   # Export to custom path
    """
    from pathlib import Path

    from gobby.config.persistence import MemoryBackupConfig
    from gobby.sync.memories import MemoryBackupManager
    from gobby.utils.project_context import get_project_context

    project_ctx = get_project_context(cwd=Path.cwd())
    project_id = project_ctx.get("id") if project_ctx else None

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

    count = backup_mgr.backup_sync(project_id=project_id)
    if not quiet:
        if count > 0:
            click.echo(f"Backed up {count} memories to {export_path}")
        else:
            click.echo("No memories to backup.")


@memory.command("restore")
@click.option(
    "--input",
    "input_path",
    type=click.Path(),
    help="Input file path (default: .gobby/memories.jsonl)",
)
@click.option("--quiet", "-q", is_flag=True, help="Suppress output")
@click.pass_context
def restore_memories(ctx: click.Context, input_path: str | None, quiet: bool) -> None:
    """Restore memories from a JSONL backup file.

    Imports memories from a JSONL file into the database. This runs synchronously
    and is the explicit CLI path for reading .gobby/memories.jsonl.

    Examples:

        gobby memory restore

        gobby memory restore --input ~/backups/mem.jsonl
    """
    from pathlib import Path

    from gobby.config.persistence import MemoryBackupConfig
    from gobby.sync.memories import MemoryBackupManager

    manager = get_memory_manager(ctx)
    restore_path = Path(input_path) if input_path else Path(".gobby/memories.jsonl")
    config = MemoryBackupConfig(enabled=True, export_path=restore_path)
    backup_mgr = MemoryBackupManager(
        db=manager.db,
        memory_manager=manager,
        config=config,
    )

    count = backup_mgr.import_sync(force=True)
    if not quiet:
        if count > 0:
            click.echo(f"Restored {count} memories from {restore_path}")
        else:
            click.echo("No memories restored.")


@memory.command("reindex-embeddings")
@click.pass_context
def reindex_embeddings(ctx: click.Context) -> None:
    """Regenerate embeddings for all memories.

    Generates embedding vectors for all stored memories using the configured
    embedding model. Useful after changing models or for initial setup.

    Requires the Gobby daemon to be running (delegates via HTTP API).

    Examples:

        gobby memory reindex-embeddings
    """
    client = _get_daemon_client(ctx)
    click.echo("Reindexing memory embeddings...")
    try:
        response = client.call_http_api(
            "/api/memories/embeddings/reindex", method="POST", timeout=300.0
        )
        result = response.json()
    except (httpx.HTTPError, ConnectionError, OSError, ValueError) as e:
        click.echo(f"Error: Could not reach daemon — is it running? ({e})")
        raise SystemExit(1) from e

    if result.get("success", True):
        total = result.get("total_memories", 0)
        generated = result.get("embeddings_generated", 0)
        click.echo(f"Reindexed {generated}/{total} memory embeddings.")
    else:
        click.echo(f"Error: {result.get('error', 'Unknown error')}")


@memory.command("reconcile")
@click.option("--dry-run", is_flag=True, help="Report orphans without deleting")
@click.pass_context
def reconcile(ctx: click.Context, dry_run: bool) -> None:
    """Reconcile Qdrant and Neo4j with SQLite source of truth.

    Finds orphaned vectors and graph nodes whose memory IDs no longer
    exist in SQLite, and optionally deletes them.

    Requires the Gobby daemon to be running (delegates via HTTP API).

    Examples:

        gobby memory reconcile --dry-run

        gobby memory reconcile
    """
    client = _get_daemon_client(ctx)
    mode = "Dry-run: scanning" if dry_run else "Reconciling"
    click.echo(f"{mode} memory stores...")
    try:
        params = urllib.parse.urlencode({"dry_run": str(dry_run).lower()})
        response = client.call_http_api(
            f"/api/memories/reconcile?{params}", method="POST", timeout=600.0
        )
        result = response.json()
    except (httpx.HTTPError, ConnectionError, OSError, ValueError) as e:
        click.echo(f"Error: Could not reach daemon — is it running? ({e})")
        raise SystemExit(1) from e

    qdrant = result.get("qdrant", {})
    neo4j = result.get("neo4j", {})
    click.echo(f"SQLite memories: {result.get('sqlite_count', '?')}")
    click.echo(
        f"Qdrant: {qdrant.get('orphans_found', 0)} orphans found, "
        f"{qdrant.get('orphans_deleted', 0)} deleted"
    )
    click.echo(
        f"Neo4j: {neo4j.get('orphan_memories_found', 0)} orphan memories, "
        f"{neo4j.get('orphan_memories_deleted', 0)} deleted; "
        f"{neo4j.get('orphan_entities_deleted', 0)} orphan entities cleaned"
    )
    if dry_run:
        click.echo("(dry run — no changes made)")


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
    if not response.is_success:
        raise click.ClickException(f"Rebuild failed (HTTP {response.status_code}): {response.text}")
    try:
        data = response.json()
    except ValueError as e:
        raise click.ClickException(f"Invalid response from daemon: {e}") from e
    click.echo(
        f"Done: {data.get('memories_processed', '?')} memories processed, "
        f"{data.get('crossrefs_created', '?')} crossrefs created"
    )


@memory.command("clear-graph")
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def clear_graph(ctx: click.Context, project_ref: str | None, yes: bool) -> None:
    """Clear only the Neo4j knowledge graph projection (requires running daemon).

    Deletes the KG projection in Neo4j and marks affected memories pending
    so they can be extracted again. Does not clear vectors, crossrefs, or FTS.

    Examples:

        gobby memory clear-graph

        gobby memory clear-graph -p myproject

        gobby memory clear-graph --yes
    """
    client = _get_daemon_client(ctx)
    is_healthy, err = client.check_health()
    if not is_healthy:
        raise click.ClickException(f"Daemon not running: {err}")

    project_id = resolve_project_ref(project_ref, exit_on_not_found=project_ref is not None)
    if not project_id:
        if project_ref is not None:
            raise click.ClickException(
                f"Project '{project_ref}' was not found. "
                "Pass a valid -p value or check the identifier."
            )
        if not yes:
            click.confirm(
                "No project detected. This will clear the knowledge graph for ALL projects "
                "and requeue affected memories. Continue?",
                abort=True,
            )
    elif not yes:
        click.confirm(
            "This will clear the knowledge graph for this project and requeue affected "
            "memories. Continue?",
            abort=True,
        )

    params = f"?project_id={urllib.parse.quote(str(project_id))}" if project_id else ""
    click.echo("Clearing knowledge graph...")
    try:
        response = client.call_http_api(
            f"/api/memories/graph/clear{params}", method="POST", timeout=30.0
        )
    except (httpx.HTTPError, ConnectionError, OSError, ValueError) as e:
        click.echo(f"Error: Could not reach daemon — is it running? ({e})")
        raise SystemExit(1) from e

    if not response.is_success:
        raise click.ClickException(f"Clear failed (HTTP {response.status_code}): {response.text}")
    try:
        data = response.json()
    except ValueError as e:
        raise click.ClickException(f"Invalid response from daemon: {e}") from e

    scope = f"project {project_id}" if project_id else "all projects"
    click.echo(f"Knowledge graph cleared for {scope}.")
    click.echo(
        f"  Graph: {data.get('memories_deleted', 0)} memory nodes, "
        f"{data.get('entities_deleted', 0)} orphaned entities"
    )
    click.echo(f"  Requeued: {data.get('memories_marked_pending', 0)} memories")
    click.echo(
        "Use `gobby memory rebuild-graph` to repopulate immediately, or let the "
        "background worker refill it over time."
    )


@memory.command("rebuild-graph")
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.option(
    "--wait/--no-wait",
    default=False,
    help="Wait for completion by polling daemon status",
)
@click.option(
    "--timeout",
    type=int,
    default=600,
    show_default=True,
    help="Maximum seconds to wait while polling rebuild status",
)
@click.pass_context
def rebuild_graph(ctx: click.Context, project_ref: str | None, wait: bool, timeout: int) -> None:
    """Extract entities from memories into the knowledge graph (requires running daemon).

    Processes all memories through LLM entity extraction and stores
    results in Neo4j. Powers the 3D knowledge graph visualization.

    Examples:

        gobby memory rebuild-graph

        gobby memory rebuild-graph -p myproject

        gobby memory rebuild-graph --no-wait
    """
    client = _get_daemon_client(ctx)
    is_healthy, err = client.check_health()
    if not is_healthy:
        raise click.ClickException(f"Daemon not running: {err}")

    project_id = resolve_project_ref(project_ref, exit_on_not_found=True) if project_ref else None
    query_params: dict[str, str] = {"background": "true"}
    if project_id:
        query_params["project_id"] = str(project_id)
    params = f"?{urllib.parse.urlencode(query_params)}"
    response = client.call_http_api(
        f"/api/memories/graph/rebuild{params}", method="POST", timeout=30.0
    )
    if not response.is_success:
        raise click.ClickException(f"Rebuild failed (HTTP {response.status_code}): {response.text}")
    try:
        data = response.json()
    except ValueError as e:
        raise click.ClickException(f"Invalid response from daemon: {e}") from e

    job_id = data.get("job_id")
    if not job_id:
        raise click.ClickException("Invalid rebuild response: missing job_id")

    if data.get("already_running"):
        click.echo(f"Knowledge graph rebuild already running (job {job_id}).")
    else:
        click.echo(f"Started knowledge graph rebuild (job {job_id}).")

    if not wait:
        attach_cmd = "gobby memory rebuild-graph --wait"
        if project_ref:
            attach_cmd += f" -p {project_ref}"
        click.echo(f"Use `{attach_cmd}` to attach and poll progress, or check daemon logs.")
        return

    click.echo("Polling knowledge graph rebuild progress...")
    last_snapshot: tuple[object, ...] | None = None
    status_params = urllib.parse.urlencode({"job_id": str(job_id)})
    start_time = time.time()

    while True:
        elapsed = time.time() - start_time
        if elapsed > timeout:
            raise click.ClickException(f"Rebuild job {job_id} timed out after {timeout} seconds")
        status_response = client.call_http_api(
            f"/api/memories/graph/rebuild/status?{status_params}",
            method="GET",
            timeout=30.0,
        )
        if not status_response.is_success:
            raise click.ClickException(
                f"Status check failed (HTTP {status_response.status_code}): {status_response.text}"
            )
        try:
            status_data = status_response.json()
        except ValueError as e:
            raise click.ClickException(f"Invalid status response from daemon: {e}") from e

        status = status_data.get("status", "unknown")
        total = int(status_data.get("memories_total") or 0)
        completed = int(status_data.get("memories_completed") or 0)
        errors = int(status_data.get("errors") or 0)
        counts = status_data.get("status_counts") or {}
        snapshot = (
            status,
            completed,
            total,
            errors,
            counts.get("success", 0),
            counts.get("noop_no_entities", 0),
        )
        if snapshot != last_snapshot:
            if status == "running":
                click.echo(
                    f"  progress: {completed}/{total or '?'} "
                    f"(success={counts.get('success', 0)}, "
                    f"noop={counts.get('noop_no_entities', 0)}, errors={errors})"
                )
            last_snapshot = snapshot

        if status == "completed":
            failed_memories = status_data.get("failed_memories") or []
            click.echo(
                f"Done: {status_data.get('result', {}).get('memories_extracted', '?')}/"
                f"{status_data.get('result', {}).get('memories_processed', total or '?')} "
                f"memories extracted, {errors} errors"
            )
            for failure in failed_memories:
                memory_id = failure.get("memory_id", "?")
                failure_status = failure.get("status", "unknown")
                failure_errors = failure.get("errors") or []
                detail = failure_errors[0] if failure_errors else "unknown error"
                click.echo(f"  failure: {memory_id} ({failure_status}) {detail}")
            return

        if status == "failed":
            detail = status_data.get("error") or "unknown error"
            raise click.ClickException(f"Rebuild job {job_id} failed: {detail}")

        time.sleep(3)


@memory.command("invalidate")
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def invalidate(ctx: click.Context, project_ref: str | None, yes: bool) -> None:
    """Wipe and rebuild ALL memory indices (embeddings, crossrefs, graph, FTS5).

    Clears Qdrant vectors, Neo4j graph, crossrefs, and FTS5 for the
    project, then starts a background rebuild from the SQLite source of
    truth.  The command returns as soon as indices are cleared.

    Examples:

        gobby memory invalidate

        gobby memory invalidate -p myproject

        gobby memory invalidate --yes
    """
    client = _get_daemon_client(ctx)
    is_healthy, err = client.check_health()
    if not is_healthy:
        raise click.ClickException(f"Daemon not running: {err}")

    # Resolve project: auto-detect from CWD, explicit flag, or global with confirmation
    project_id = resolve_project_ref(project_ref, exit_on_not_found=project_ref is not None)

    if not project_id:
        if project_ref is not None:
            # Explicit --project that wasn't found (shouldn't reach here due to
            # exit_on_not_found, but guard anyway)
            raise click.ClickException(
                f"Project '{project_ref}' was not found. "
                "Pass a valid -p value or check the identifier."
            )
        # No project context and no explicit flag — confirm global operation
        if not yes:
            click.confirm(
                "No project detected. This will invalidate indices for ALL projects. Continue?",
                abort=True,
            )
    else:
        if not yes:
            click.confirm(
                "This will wipe and rebuild all memory indices for this project. Continue?",
                abort=True,
            )

    params = f"?project_id={urllib.parse.quote(str(project_id))}" if project_id else ""
    click.echo("Clearing memory indices...")
    try:
        response = client.call_http_api(
            f"/api/memories/invalidate{params}", method="POST", timeout=30.0
        )
    except (httpx.HTTPError, ConnectionError, OSError, ValueError) as e:
        click.echo(f"Error: Could not reach daemon — is it running? ({e})")
        raise SystemExit(1) from e

    if not response.is_success:
        raise click.ClickException(
            f"Invalidate failed (HTTP {response.status_code}): {response.text}"
        )
    try:
        data = response.json()
    except ValueError as e:
        raise click.ClickException(f"Invalid response from daemon: {e}") from e

    graph_cleared = data.get("graph_cleared", {})
    if graph_cleared and not graph_cleared.get("skipped"):
        click.echo(
            f"  Graph: {graph_cleared.get('memories_deleted', 0)} memory nodes, "
            f"{graph_cleared.get('entities_deleted', 0)} orphaned entities"
        )
    if data.get("vectors_cleared"):
        click.echo("  Vectors: cleared")
    crossrefs = data.get("crossrefs_cleared")
    if crossrefs is not None:
        click.echo(f"  Crossrefs: {crossrefs} deleted")
    if data.get("fts_cleared"):
        click.echo("  FTS5: cleared")

    click.echo("Indices cleared. Rebuild started in background.")
    click.echo("Check daemon logs for rebuild progress.")


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
