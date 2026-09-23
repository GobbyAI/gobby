"""Launch addressing for a node: one default workspace per project, many scratches."""

from __future__ import annotations

from uuid import uuid4

from psycopg.errors import ForeignKeyViolation, UniqueViolation

from gobby.storage.hub.protocol import Transaction
from gobby.storage.workspaces import (
    DEFAULT_WORKSPACE_NAME,
    InvalidWorkspaceOpError,
    Workspace,
    WorkspaceManager,
    WorkspaceNotFoundError,
    _free_ref,
    _lock_rows,
    _required,
    _uuid,
    _workspace_name,
)

_INSERT_ATTEMPTS = 8


def resolve_launch_workspace(
    workspaces: WorkspaceManager,
    machine_id: str,
    *,
    workspace: str | None,
    project_id: str | None,
) -> tuple[Workspace, bool]:
    """Return the workspace a launch attaches and whether this call created it.

    An explicit workspace wins, including when a project is also named. A
    registered project resolves or creates its one default workspace. Neither
    resolves the projectless scratch named ``default``.
    """
    if workspace is not None:
        return workspaces.resolve_reference(workspace, node=machine_id).workspace, False
    if project_id is None:
        return workspaces.create(machine_id, DEFAULT_WORKSPACE_NAME)
    return _ensure_project_workspace(workspaces, machine_id, project_id)


def _ensure_project_workspace(
    workspaces: WorkspaceManager, machine_id: str, project_id: str
) -> tuple[Workspace, bool]:
    machine_id = _uuid(machine_id)
    project_id = _uuid(project_id)
    base_name = _project_workspace_name(workspaces, project_id)
    with workspaces.db.transaction() as conn:
        _lock_rows(conn, "machines", machine_id)
        for _ in range(_INSERT_ATTEMPTS):
            existing = conn.execute(
                """
                SELECT * FROM workspaces
                WHERE machine_id = %s AND default_project_id = %s
                """,
                (machine_id, project_id),
            ).fetchone()
            if existing is not None:
                return Workspace.from_row(existing), False
            name = _vacant_name(conn, machine_id, base_name)
            savepoint = conn.savepoint("project_workspace")
            try:
                row = conn.execute(
                    """
                    INSERT INTO workspaces (
                        id, machine_id, ref, name, default_project_id
                    ) VALUES (%s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        str(uuid4()),
                        machine_id,
                        _free_ref(conn, "workspaces", machine_id),
                        name,
                        project_id,
                    ),
                ).fetchone()
            except UniqueViolation:
                savepoint.rollback()
                continue
            except ForeignKeyViolation as exc:
                savepoint.rollback()
                raise WorkspaceNotFoundError(f"Project {project_id} not found") from exc
            savepoint.release()
            return Workspace.from_row(_required(row, f"Workspace for {project_id}")), True
    raise InvalidWorkspaceOpError(
        f"Could not create the default workspace for project {project_id}"
    )


def _project_workspace_name(workspaces: WorkspaceManager, project_id: str) -> str:
    row = workspaces.db.fetchone("SELECT name FROM projects WHERE id = %s", (project_id,))
    if row is None or row["name"] is None:
        raise WorkspaceNotFoundError(f"Project {project_id} not found")
    try:
        return _workspace_name(str(row["name"]))
    except InvalidWorkspaceOpError:
        return _workspace_name(f"project-{project_id.split('-', 1)[0]}")


def _vacant_name(conn: Transaction, machine_id: str, base_name: str) -> str:
    name = base_name
    for suffix in range(2, _INSERT_ATTEMPTS + 2):
        taken = conn.execute(
            "SELECT 1 FROM workspaces WHERE machine_id = %s AND name = %s",
            (machine_id, name),
        ).fetchone()
        if taken is None:
            return name
        name = _workspace_name(f"{base_name} {suffix}")
    raise InvalidWorkspaceOpError(f"No free workspace name near {base_name!r}")
