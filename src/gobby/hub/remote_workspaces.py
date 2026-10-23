"""Hub access to worktree and clone rows that a node owns.

Rows are read and written by id with no machine guard. Every call that reaches a
node is ownership-checked first.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from gobby.hub.caller_scope import require_machine_owned
from gobby.hub.machine_routing import is_local_machine
from gobby.hub.node_client import NodeCommandError, node_command_client
from gobby.hub.node_commands import MergePayload, MergeResult, RemovePayload, RemoveResult
from gobby.storage.clones import Clone
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.worktrees import _UUID_TEXT_PREFIX_RE, Worktree, _escape_like_prefix
from gobby.utils.uuid_validation import parse_uuid_reference

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry

WorkspaceKind = Literal["worktree", "clone"]
Workspace = Worktree | Clone
Owner = tuple[str, str]

__all__ = [
    "WorkspaceKind",
    "get_remote",
    "list_remote",
    "machine_refusal",
    "merge_remote",
    "node_command_client",
    "node_error_result",
    "owner_machine",
    "remove_remote",
    "route_node_workspace_tools",
]

_TABLE: dict[WorkspaceKind, str] = {"worktree": "worktrees", "clone": "clones"}
_CLEANUP_DAYS: dict[WorkspaceKind, int] = {"worktree": 0, "clone": 7}
_REMOVE_REFUSALS = {"dirty": "dirty", "unmerged": "unmerged", "busy": "workspace_busy"}
_MERGE_TIMEOUT_MS = 250_000


def owner_machine(db: HubDatabase, kind: WorkspaceKind, ref: str) -> Owner | None:
    """Return ``(full_id, machine_id)`` for the one row ``ref`` names on any machine."""
    text = ref.strip()
    full = parse_uuid_reference(text)
    if full is not None:
        sql = f"SELECT id, machine_id FROM {_TABLE[kind]} WHERE id = %s"  # nosec B608
        rows = db.fetchall(sql, (str(full),))
    elif kind == "worktree" and _UUID_TEXT_PREFIX_RE.fullmatch(text) is not None:
        sql = (
            "SELECT id, machine_id FROM worktrees WHERE id::text LIKE %s ESCAPE '\\' "
            "ORDER BY id LIMIT 2"
        )
        rows = db.fetchall(sql, (f"{_escape_like_prefix(text)}%",))
    else:
        return None
    if len(rows) != 1:
        return None
    return str(rows[0]["id"]), str(rows[0]["machine_id"])


def get_remote(db: HubDatabase, kind: WorkspaceKind, workspace_id: str) -> Workspace | None:
    sql = f"SELECT * FROM {_TABLE[kind]} WHERE id = %s"  # nosec B608
    row = db.fetchone(sql, (workspace_id,))
    return None if row is None else _from_row(kind, row)


def list_remote(
    db: HubDatabase,
    kind: WorkspaceKind,
    machine_id: str,
    project_id: str | None = None,
    status: str | None = None,
) -> list[Any]:
    conditions = ["machine_id = %s"]
    params: list[Any] = [machine_id]
    if project_id:
        conditions.append("project_id = %s")
        params.append(project_id)
    if status:
        conditions.append("status = %s")
        params.append(status)
    where_clause = " AND ".join(conditions)
    sql = f"SELECT * FROM {_TABLE[kind]} WHERE {where_clause} ORDER BY created_at DESC"  # nosec B608
    rows = db.fetchall(sql, tuple(params))
    return [_from_row(kind, row) for row in rows]


async def merge_remote(
    db: HubDatabase,
    kind: WorkspaceKind,
    row: Workspace,
    *,
    target_branch: str | None,
    client: Any,
) -> dict[str, Any]:
    target = target_branch or row.base_branch
    if not target:
        return _refused("target_branch_required")
    if row.branch_name is None:
        return _refused("branch_required")
    source_root = _source_root(db, row.machine_id, row.project_id)
    if source_root is None:
        return _refused("checkout_unresolved")
    payload = MergePayload(
        project_id=row.project_id,
        mode=kind,
        path=_path(row),
        branch_name=row.branch_name,
        target_branch=target,
        source_root=source_root,
    ).model_dump()
    sent = await client.send(
        row.machine_id, "workspace.merge", payload, timeout_ms=_MERGE_TIMEOUT_MS
    )
    reply = MergeResult.model_validate(sent)
    if reply.outcome in ("merged", "already_merged"):
        _mark_merged(db, kind, row)
        return {
            "success": True,
            "merged": True,
            "target_branch": target,
            "merge_sha": reply.merge_sha,
            "target_head_sha": reply.target_head_sha,
            "pushed": False,
            "machine_id": row.machine_id,
        }
    if reply.outcome == "conflict":
        return {
            "success": False,
            "error_code": "conflict",
            "merged": False,
            "has_conflicts": True,
            "conflicted_files": reply.conflicted_files,
        }
    if reply.outcome == "target_dirty":
        return {"success": False, "error_code": "target_dirty", "dirty_paths": reply.dirty_paths}
    return _refused("workspace_busy")


async def remove_remote(
    db: HubDatabase,
    kind: WorkspaceKind,
    row: Workspace,
    *,
    force: bool,
    force_delete_branch: bool,
    merged_into: str | None,
    client: Any,
) -> dict[str, Any]:
    if row.branch_name is None:
        return _refused("branch_required")
    source_root = _source_root(db, row.machine_id, row.project_id)
    if source_root is None:
        return _refused("checkout_unresolved")
    payload = RemovePayload(
        project_id=row.project_id,
        mode=kind,
        path=_path(row),
        branch_name=row.branch_name,
        source_root=source_root,
        force=force,
        force_delete_branch=force_delete_branch,
        merged_into=merged_into,
    ).model_dump()
    sent = await client.send(row.machine_id, "workspace.remove", payload)
    reply = RemoveResult.model_validate(sent)
    if reply.outcome == "removed":
        _delete_row(db, kind, row)
        return {"success": True, "machine_id": row.machine_id}
    return _refused(_REMOVE_REFUSALS[reply.outcome])


def machine_refusal(machine_id: str | None) -> dict[str, Any] | None:
    """Return the refusal for a machine the caller does not own, or None when allowed."""
    if machine_id is None:
        return None
    try:
        require_machine_owned(machine_id)
    except NodeCommandError as exc:
        return node_error_result(exc)
    return None


def node_error_result(exc: NodeCommandError) -> dict[str, Any]:
    return {"success": False, "error": str(exc), "error_code": exc.code, "stage": exc.stage}


def route_node_workspace_tools(
    registry: InternalToolRegistry, *, kind: WorkspaceKind, db: HubDatabase
) -> None:
    """Route get, merge and delete of one kind to the owning node when it is remote."""
    routes = (("get", _remote_get), ("merge", _remote_merge), ("delete", _remote_delete))
    for op, remote in routes:
        name = f"{op}_{kind}"
        tool = registry.get_tool_metadata(name)
        if tool is None:
            raise LookupError(f"{name} is not registered on {registry.name}")
        registry.register(
            name,
            tool.description,
            tool.input_schema,
            _routed(tool.func, kind=kind, db=db, remote=remote),
            brief=tool.brief,
            output_schema=tool.output_schema,
            read_only=tool.read_only,
        )


def _routed(
    original: Callable[..., Any],
    *,
    kind: WorkspaceKind,
    db: HubDatabase,
    remote: Callable[..., Any],
) -> Callable[..., Any]:
    """Ownership first; a remote row goes to its node, anything else to the original."""
    if inspect.iscoroutinefunction(original):

        @functools.wraps(original)
        async def routed_async(**kwargs: Any) -> Any:
            try:
                owner = _owned(db, kind, kwargs)
                if owner is None or is_local_machine(owner[1]):
                    return await original(**kwargs)
                result = remote(db, kind, kwargs, owner)
                return await result if inspect.isawaitable(result) else result
            except NodeCommandError as exc:
                return node_error_result(exc)

        return routed_async

    @functools.wraps(original)
    def routed_sync(**kwargs: Any) -> Any:
        try:
            owner = _owned(db, kind, kwargs)
            if owner is None or is_local_machine(owner[1]):
                return original(**kwargs)
            return remote(db, kind, kwargs, owner)
        except NodeCommandError as exc:
            return node_error_result(exc)

    return routed_sync


def _owned(db: HubDatabase, kind: WorkspaceKind, kwargs: dict[str, Any]) -> Owner | None:
    ref = kwargs.get(f"{kind}_id")
    if not isinstance(ref, str):
        return None
    owner = owner_machine(db, kind, ref)
    if owner is not None:
        require_machine_owned(owner[1])
    return owner


def _remote_get(
    db: HubDatabase, kind: WorkspaceKind, kwargs: dict[str, Any], owner: Owner
) -> dict[str, Any]:
    row = get_remote(db, kind, owner[0])
    if row is None:
        return _not_found(kind, owner[0])
    if kind == "worktree":
        return {"success": True, "worktree": row.to_dict(), "git_status": None}
    return {"success": True, "clone": {**row.to_dict(), "disk_exists": None}}


async def _remote_merge(
    db: HubDatabase, kind: WorkspaceKind, kwargs: dict[str, Any], owner: Owner
) -> dict[str, Any]:
    row = get_remote(db, kind, owner[0])
    if row is None:
        return _not_found(kind, owner[0])
    if _flag(kwargs.get("push")) or _flag(kwargs.get("prefer_remote")):
        return _refused("remote_merge_option_unsupported")
    source = kwargs.get("source_branch")
    if source is not None and source != row.branch_name:
        return _refused("remote_merge_option_unsupported")
    return await merge_remote(
        db,
        kind,
        row,
        target_branch=kwargs.get("target_branch"),
        client=node_command_client(),
    )


async def _remote_delete(
    db: HubDatabase, kind: WorkspaceKind, kwargs: dict[str, Any], owner: Owner
) -> dict[str, Any]:
    row = get_remote(db, kind, owner[0])
    if row is None:
        return _not_found(kind, owner[0])
    return await remove_remote(
        db,
        kind,
        row,
        force=_flag(kwargs.get("force")),
        force_delete_branch=_flag(kwargs.get("force_delete_branch")),
        merged_into=kwargs.get("merged_into"),
        client=node_command_client(),
    )


def _mark_merged(db: HubDatabase, kind: WorkspaceKind, row: Workspace) -> None:
    now = datetime.now(UTC)
    cleanup_after = now + timedelta(days=_CLEANUP_DAYS[kind])
    if isinstance(row, Worktree):
        db.execute(
            "UPDATE worktrees SET status = 'merged', merged_at = %s, cleanup_after = %s, "
            "updated_at = %s WHERE id = %s AND machine_id = %s",
            (now, cleanup_after, now, row.id, row.machine_id),
        )
        return
    db.execute(
        "UPDATE clones SET status = 'merged', cleanup_after = %s, updated_at = %s "
        "WHERE id = %s AND machine_id = %s",
        (cleanup_after, now, row.id, row.machine_id),
    )


def _delete_row(db: HubDatabase, kind: WorkspaceKind, row: Workspace) -> None:
    with db.transaction() as conn:
        if isinstance(row, Worktree):
            conn.execute(
                "UPDATE sessions SET workspace_path = NULL, "
                "workspace_generation = workspace_generation + 1, updated_at = NOW() "
                "WHERE workspace_path = %s AND machine_id = %s",
                (row.worktree_path, row.machine_id),
            )
        sql = f"DELETE FROM {_TABLE[kind]} WHERE id = %s AND machine_id = %s"  # nosec B608
        conn.execute(sql, (row.id, row.machine_id))


def _source_root(db: HubDatabase, machine_id: str, project_id: str) -> str | None:
    checkout = LocalProjectCheckoutManager(db).get(machine_id, project_id)
    return None if checkout is None else checkout.root_path


def _from_row(kind: WorkspaceKind, row: Any) -> Workspace:
    return Worktree.from_row(row) if kind == "worktree" else Clone.from_row(row)


def _path(row: Workspace) -> str:
    return row.worktree_path if isinstance(row, Worktree) else row.clone_path


def _refused(code: str) -> dict[str, Any]:
    return {"success": False, "error_code": code}


def _not_found(kind: WorkspaceKind, workspace_id: str) -> dict[str, Any]:
    return {"success": False, "error": f"{kind} '{workspace_id}' not found"}


def _flag(value: Any) -> bool:
    return value in (True, "true", "True", "1")
