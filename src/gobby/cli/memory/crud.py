from __future__ import annotations

import asyncio
import importlib
from types import ModuleType

import click

from ._formatting import format_tags, parse_tags, truncate


def _facade() -> ModuleType:
    return importlib.import_module("gobby.cli.memory")


@click.command()
@click.argument("content")
@click.option(
    "--type", "-t", "memory_type", default="fact", help="Type of memory (fact, preference, etc.)"
)
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.pass_context
def create(ctx: click.Context, content: str, memory_type: str, project_ref: str | None) -> None:
    """Create a new memory."""
    memory_module = _facade()
    project_id = memory_module.resolve_project_ref(project_ref) if project_ref else None
    manager = memory_module.get_memory_manager(ctx)
    memory = asyncio.run(
        manager.create_memory(
            content=content,
            memory_type=memory_type,
            project_id=project_id,
            source_type="user",
        )
    )
    click.echo(f"Created memory: {memory.id} - {memory.content}")


@click.command()
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
    memory_module = _facade()
    project_id = memory_module.resolve_project_ref(project_ref) if project_ref else None
    manager = memory_module.get_memory_manager(ctx)

    memories = asyncio.run(
        manager.search_memories(
            query=query,
            project_id=project_id,
            limit=limit,
            tags_all=parse_tags(tags_all),
            tags_any=parse_tags(tags_any),
            tags_none=parse_tags(tags_none),
        )
    )
    if not memories:
        click.echo("No memories found.")
        return

    for mem in memories:
        click.echo(f"[{mem.id[:8]}] ({mem.memory_type}){format_tags(mem.tags)} {mem.content}")


@click.command()
@click.argument("memory_ref")
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.pass_context
def delete(ctx: click.Context, memory_ref: str, project_ref: str | None = None) -> None:
    """Delete a memory by ID (UUID or prefix)."""
    memory_module = _facade()
    project_id = memory_module.resolve_project_ref(project_ref) if project_ref else None
    manager = memory_module.get_memory_manager(ctx)
    try:
        memory_id = memory_module.resolve_memory_id(manager, memory_ref, project_id=project_id)
    except click.ClickException as e:
        click.echo(f"Error: {e.message}")
        return
    success = asyncio.run(manager.delete_memory(memory_id))
    if success:
        click.echo(f"Deleted memory: {memory_id}")
    else:
        click.echo(f"Memory not found: {memory_id}")


@click.command("list")
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
    memory_module = _facade()
    project_id = memory_module.resolve_project_ref(project_ref) if project_ref else None
    manager = memory_module.get_memory_manager(ctx)

    memories = manager.list_memories(
        project_id=project_id,
        memory_type=memory_type,
        limit=limit,
        tags_all=parse_tags(tags_all),
        tags_any=parse_tags(tags_any),
        tags_none=parse_tags(tags_none),
    )
    if not memories:
        click.echo("No memories found.")
        return

    for mem in memories:
        click.echo(f"[{mem.id[:8]}] ({mem.memory_type}){format_tags(mem.tags)}")
        click.echo(f"  {truncate(mem.content, 100)}")


@click.command("show")
@click.argument("memory_ref")
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.pass_context
def show_memory(ctx: click.Context, memory_ref: str, project_ref: str | None = None) -> None:
    """Show details of a specific memory (UUID or prefix)."""
    memory_module = _facade()
    project_id = memory_module.resolve_project_ref(project_ref) if project_ref else None
    manager = memory_module.get_memory_manager(ctx)
    try:
        memory_id = memory_module.resolve_memory_id(manager, memory_ref, project_id=project_id)
    except click.ClickException as e:
        click.echo(f"Error: {e.message}")
        return
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


@click.command("update")
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
    memory_module = _facade()
    project_id = memory_module.resolve_project_ref(project_ref) if project_ref else None
    manager = memory_module.get_memory_manager(ctx)
    try:
        memory_id = memory_module.resolve_memory_id(manager, memory_ref, project_id=project_id)
    except click.ClickException as e:
        click.echo(f"Error: {e.message}")
        return
    tag_list = parse_tags(tags, empty_as_none=True)

    try:
        memory = asyncio.run(
            manager.update_memory(
                memory_id=memory_id,
                content=content,
                tags=tag_list,
            )
        )
        click.echo(f"Updated memory: {memory.id}")
        click.echo(f"  Content: {truncate(memory.content, 80)}")
    except ValueError as e:
        click.echo(f"Error: {e}")


@click.command("stats")
@click.option("--project", "-p", "project_ref", help="Project (name or UUID)")
@click.pass_context
def memory_stats(ctx: click.Context, project_ref: str | None) -> None:
    """Show memory system statistics."""
    memory_module = _facade()
    project_id = memory_module.resolve_project_ref(project_ref) if project_ref else None
    manager = memory_module.get_memory_manager(ctx)
    stats = manager.get_stats(project_id=project_id)

    click.echo("Memory Statistics:")
    click.echo(f"  Total Memories: {stats['total_count']}")
    if stats["by_type"]:
        click.echo("  By Type:")
        for mem_type, count in stats["by_type"].items():
            click.echo(f"    {mem_type}: {count}")
