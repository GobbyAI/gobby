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

    from gobby.storage.hub.runtime import open_runtime_hub_database

    db = open_runtime_hub_database(apply_migrations=False)
    try:
        manager = deps.LocalProjectManager(db)

        project = manager.get(project_ref)
        if project:
            return str(project.id)

        project = manager.get_by_name(project_ref)
        if project:
            return str(project.id)
    finally:
        db.close()

    if exit_on_not_found:
        click.echo(f"Project not found: {project_ref}", err=True)
        raise SystemExit(1)
    return None


def get_active_session_id(db: HubDatabase | None = None) -> str | None:
    """Get the most recent active session ID."""
    close_db = False
    if db is None:
        from gobby.storage.hub.runtime import open_runtime_hub_database

        db = open_runtime_hub_database(apply_migrations=False)
        close_db = True

    try:
        row = db.fetchone(
            "SELECT id FROM sessions WHERE status = 'active' AND source != 'system' "
            "ORDER BY updated_at DESC LIMIT 1"
        )
        return str(row["id"]) if row else None
    finally:
        if close_db:
            db.close()


def resolve_session_id(session_ref: str | None, project_id: str | None = None) -> str:
    """Resolve session reference to UUID."""
    deps = facade()

    from gobby.storage.hub.runtime import open_runtime_hub_database

    db = open_runtime_hub_database(apply_migrations=False)
    try:
        if not session_ref:
            active_id = deps.get_active_session_id(db)
            if not active_id:
                raise click.ClickException("No active session found. Specify --session.")
            return str(active_id)

        if not project_id:
            ctx = deps.get_project_context(cwd=deps.Path.cwd())
            project_id = str(ctx.get("id")) if ctx and ctx.get("id") else None

        manager = deps.SessionManager(db)
        try:
            return str(manager.resolve_session_reference(session_ref, project_id))
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
    finally:
        db.close()


def list_project_names() -> list[str]:
    """List all project names for shell completion."""
    deps = facade()

    from gobby.storage.hub.runtime import open_runtime_hub_database

    db = open_runtime_hub_database(apply_migrations=False)
    try:
        manager = deps.LocalProjectManager(db)
        return [str(project.name) for project in manager.list()]
    finally:
        db.close()
