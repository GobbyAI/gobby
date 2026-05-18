from __future__ import annotations

import click

from .crud import create, delete, list_memories, memory_stats, recall, show_memory, update_memory
from .export import backup_memories, export_memories, restore_memories
from .graph import clear_graph, invalidate, rebuild_graph
from .indices import rebuild_crossrefs, reconcile, reindex_embeddings
from .maintenance import dedupe_memories, fix_null_project


@click.group()
def memory() -> None:
    """Manage Gobby memories."""
    pass


memory.add_command(create)
memory.add_command(recall)
memory.add_command(delete)
memory.add_command(list_memories)
memory.add_command(show_memory)
memory.add_command(update_memory)
memory.add_command(memory_stats)
memory.add_command(export_memories)
memory.add_command(dedupe_memories)
memory.add_command(fix_null_project)
memory.add_command(backup_memories)
memory.add_command(restore_memories)
memory.add_command(reindex_embeddings)
memory.add_command(reconcile)
memory.add_command(rebuild_crossrefs)
memory.add_command(clear_graph)
memory.add_command(rebuild_graph)
memory.add_command(invalidate)
