"""Compatibility handlers for removed install options."""

import click

_GRAPH_BACKEND_REMOVED_MESSAGE = """--neo4j / --neo4j-password has been removed in 0.4.0.

The knowledge graph backend has been replaced with FalkorDB.
- Install the required stack: printf '%s' "$PASSWORD" | gobby install --config-only --falkordb-password-stdin
- Uninstall: gobby uninstall
- Migration notes: see CHANGELOG.md for the full upgrade path."""


def _raise_graph_backend_removed() -> None:
    raise click.UsageError(_GRAPH_BACKEND_REMOVED_MESSAGE)
