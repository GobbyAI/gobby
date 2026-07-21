"""Project and session reference resolution helpers for CLI utilities."""

from __future__ import annotations

from typing import TYPE_CHECKING

import click

from gobby.cli.utils_runtime import facade

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


def resolve_project_ref(project_ref: str | None, exit_on_not_found: bool = True) -> str | None:
    """Resolve a project reference (name or UUID) to project ID."""
    deps = facade()

    if not project_ref:
        ctx = deps.get_project_context(cwd=deps.Path.cwd())
        if not ctx:
            return None
        project_id = ctx.get("id")
        return str(project_id) if project_id else None

    from gobby.cli.runtime import require_cli_database

    manager = deps.LocalProjectManager(require_cli_database())

    project = manager.get(project_ref)
    if project:
        return str(project.id)

    project = manager.get_by_name(project_ref)
    if project:
        return str(project.id)

    if exit_on_not_found:
        click.echo(f"Project not found: {project_ref}", err=True)
        raise SystemExit(1)
    return None


def get_active_session_id(
    db: HubDatabase | None = None,
    *,
    project_id: str | None = None,
) -> str | None:
    """Get the most recent active session ID."""
    if db is None:
        from gobby.cli.runtime import require_cli_database

        db = require_cli_database()

    if project_id:
        row = db.fetchone(
            "SELECT id FROM sessions "
            "WHERE status = 'active' AND source != 'system' AND project_id = %s "
            "ORDER BY updated_at DESC LIMIT 1",
            (project_id,),
        )
    else:
        row = db.fetchone(
            "SELECT id FROM sessions WHERE status = 'active' AND source != 'system' "
            "ORDER BY updated_at DESC LIMIT 1"
        )
    return str(row["id"]) if row else None


def resolve_session_id(session_ref: str | None, project_id: str | None = None) -> str:
    """Resolve session reference to UUID."""
    deps = facade()

    from gobby.cli.runtime import require_cli_database

    db = require_cli_database()
    if not project_id:
        ctx = deps.get_project_context(cwd=deps.Path.cwd())
        project_id = str(ctx.get("id")) if ctx and ctx.get("id") else None

    if not session_ref:
        active_id = deps.get_active_session_id(db, project_id=project_id)
        if not active_id:
            raise click.ClickException("No active session found. Specify --session.")
        return str(active_id)

    manager = deps.SessionManager(db)
    try:
        return str(manager.resolve_session_reference(session_ref, project_id))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None


def list_project_names() -> list[str]:
    """List all project names for shell completion."""
    deps = facade()

    from gobby.cli.runtime import require_cli_database

    manager = deps.LocalProjectManager(require_cli_database())
    return [str(project.name) for project in manager.list()]
