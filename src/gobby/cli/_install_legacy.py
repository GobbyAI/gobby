"""Compatibility handlers for removed install options."""

import click

_GRAPH_BACKEND_REMOVED_MESSAGE = """--neo4j / --neo4j-password has been removed in 0.4.0.

The knowledge graph backend has been replaced with FalkorDB.
- Install (auto-runs as part of gobby install; tune with): printf '%s' "$PASSWORD" | gobby install --falkordb-password-stdin (or service-only: add --falkordb)
- Uninstall: gobby uninstall
- Migration notes: see CHANGELOG.md for the full upgrade path."""


def _raise_graph_backend_removed() -> None:
    raise click.UsageError(_GRAPH_BACKEND_REMOVED_MESSAGE)
