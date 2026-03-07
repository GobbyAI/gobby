"""CLI commands for code indexing.

Runs standalone — no daemon required. Constructs CodeIndexer directly
against the local SQLite database, same pattern as `gobby memory`.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from typing import TYPE_CHECKING

import click

from gobby.config.app import DaemonConfig

if TYPE_CHECKING:
    from gobby.code_index.indexer import CodeIndexer
    from gobby.code_index.storage import CodeIndexStorage


def _get_indexer(ctx: click.Context) -> CodeIndexer:
    """Construct a CodeIndexer directly (no daemon needed)."""
    from gobby.code_index.indexer import CodeIndexer
    from gobby.code_index.parser import CodeParser
    from gobby.code_index.storage import CodeIndexStorage
    from gobby.storage.database import LocalDatabase

    config: DaemonConfig = ctx.obj["config"]
    db = LocalDatabase()
    ci_config = config.code_index
    storage = CodeIndexStorage(db)
    parser = CodeParser(ci_config)
    return CodeIndexer(storage=storage, parser=parser, config=ci_config)


def _get_storage() -> CodeIndexStorage:
    """Construct CodeIndexStorage directly (no daemon needed)."""
    from gobby.code_index.storage import CodeIndexStorage
    from gobby.storage.database import LocalDatabase

    db = LocalDatabase()
    return CodeIndexStorage(db)


def _auto_project_id() -> str:
    """Auto-detect project ID from .gobby/project.json, fallback to 'default'."""
    from gobby.utils.project_context import get_project_context

    ctx = get_project_context()
    if ctx and ctx.get("id"):
        return str(ctx["id"])
    return "default"


def _git_repo_root() -> str | None:
    """Get git repo root via `git rev-parse --show-toplevel`, or None."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


class _IndexGroup(click.Group):
    """Click group that defaults to the 'index' subcommand.

    - ``gobby index`` (no args) → runs the index subcommand
    - ``gobby index src/`` (path) → routes to index subcommand
    - ``gobby index --full`` (option) → routes to index subcommand
    - ``gobby index status`` (known subcommand) → normal routing
    - ``gobby index --help`` → shows group help
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        # Inject default 'index' subcommand unless a known subcommand or --help is given
        if not args or (args[0] not in self.commands and args[0] not in ("--help", "-h")):
            args = ["index"] + list(args)
        return super().parse_args(ctx, args)


@click.group("index", cls=_IndexGroup)
def code_index() -> None:
    """Code indexing commands.

    Defaults to indexing the current git repo when run without a subcommand.

    \b
    Examples:
        gobby index                  # Index current git repo
        gobby index src/             # Index specific path
        gobby index --full           # Full re-index
        gobby index status           # Show stats
        gobby index invalidate       # Clear index
    """
    pass


@code_index.command("index", hidden=True)
@click.argument("path", required=False, default=None)
@click.option("--project-id", "-p", default="", help="Project ID (default: auto-detect)")
@click.option("--full", is_flag=True, help="Full re-index (skip incremental)")
@click.pass_context
def index_cmd(ctx: click.Context, path: str | None, project_id: str, full: bool) -> None:
    """Index a directory for code navigation.

    Indexes all supported source files in PATH, extracting symbols
    (functions, classes, methods) for code navigation and search.

    Incremental by default — only re-indexes changed files.
    Defaults to git repo root when no path is given.

    Examples:

        gobby index

        gobby index src/

        gobby index . --full

        gobby index /path/to/project -p my-project
    """
    if path is None:
        path = _git_repo_root() or "."

    abs_path = os.path.abspath(path)
    if not os.path.isdir(abs_path):
        raise click.ClickException(f"Not a directory: {abs_path}")

    pid = project_id or _auto_project_id()
    incremental = not full
    click.echo(f"Indexing {abs_path} ({'incremental' if incremental else 'full'})...")

    indexer = _get_indexer(ctx)
    result = asyncio.run(
        indexer.index_directory(
            root_path=abs_path,
            project_id=pid,
            incremental=incremental,
        )
    )

    click.echo(f"Done in {result.duration_ms}ms:")
    click.echo(f"  Files indexed: {result.files_indexed}")
    click.echo(f"  Files skipped: {result.files_skipped}")
    click.echo(f"  Symbols found: {result.symbols_found}")

    if result.errors:
        click.echo(f"  Errors: {len(result.errors)}")
        for err in result.errors[:5]:
            click.echo(f"    - {err}")


@code_index.command("status")
@click.option("--project-id", "-p", default="", help="Project ID")
@click.pass_context
def status_cmd(ctx: click.Context, project_id: str) -> None:
    """Show indexing status.

    Without --project-id, lists all indexed projects.
    With --project-id, shows detailed stats for that project.

    Examples:

        gobby index status

        gobby index status -p my-project
    """
    storage = _get_storage()
    pid = project_id or ""

    if not pid:
        projects = storage.list_indexed_projects()
        if not projects:
            click.echo("No indexed projects.")
            return
        click.echo(f"Indexed projects ({len(projects)}):")
        for proj in projects:
            click.echo(f"  {proj.id}: {proj.total_files} files, {proj.total_symbols} symbols")
            if proj.last_indexed_at:
                click.echo(f"    Last indexed: {proj.last_indexed_at}")
    else:
        stats = storage.get_project_stats(pid)
        if stats is None:
            click.echo(f"Project '{pid}' is not indexed.")
            return
        click.echo(f"Project: {stats.id}")
        click.echo(f"  Root: {stats.root_path}")
        click.echo(f"  Files: {stats.total_files}")
        click.echo(f"  Symbols: {stats.total_symbols}")
        if stats.last_indexed_at:
            click.echo(f"  Last indexed: {stats.last_indexed_at}")
        if stats.index_duration_ms:
            click.echo(f"  Duration: {stats.index_duration_ms}ms")


@code_index.command("invalidate")
@click.option("--project-id", "-p", default="", help="Project ID (default: auto-detect)")
@click.pass_context
def invalidate_cmd(ctx: click.Context, project_id: str) -> None:
    """Clear index for a project, forcing full re-index next time.

    Examples:

        gobby index invalidate

        gobby index invalidate -p my-project
    """
    pid = project_id or _auto_project_id()
    indexer = _get_indexer(ctx)
    asyncio.run(indexer.invalidate(pid))
    click.echo(f"Index invalidated for project: {pid}")
