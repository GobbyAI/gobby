"""Hub access to worktree and clone rows that a node owns.

Rows are read and written by id with no machine guard. Every call that reaches a
node is ownership-checked first.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from gobby.hub.caller_scope import current_caller, require_machine_owned
from gobby.hub.machine_routing import is_local_machine
from gobby.hub.node_client import NodeCommandError, node_command_client
from gobby.hub.node_commands import MergePayload, MergeResult, RemovePayload, RemoveResult
from gobby.storage.clones import Clone
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.isolation_cleanup import CLOSED_TASK_CLEANUP_PREDICATE
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.worktrees import _UUID_TEXT_PREFIX_RE, Worktree, _escape_like_prefix
from gobby.utils.machine_id import require_machine_id
from gobby.utils.uuid_validation import parse_uuid_reference

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry

WorkspaceKind = Literal["worktree", "clone"]
Workspace = Worktree | Clone
Owner = tuple[str, str]

__all__ = [
    "WorkspaceKind",
    "expire_remote_workspaces",
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
_LIST_TOOLS = frozenset({"list_worktrees", "list_clones"})
logger = logging.getLogger(__name__)


async def expire_remote_workspaces(
    db: HubDatabase, *, run_db: Callable[..., Awaitable[Any]] | None
) -> None:
    """Ask owning nodes to safely remove expired merged workspaces."""
    runner = run_db or asyncio.to_thread
    rows = await runner(_expired_remote_rows, db)
    if not rows:
        return
    client = node_command_client()
    for kind, row in rows:
        try:
            result = await remove_remote(
                db,
                kind,
                row,
                force=False,
                force_delete_branch=False,
                merged_into=None,
                client=client,
            )
            if not result["success"]:
                logger.debug("Skipped expired remote %s %s: %s", kind, row.id, result)
        except NodeCommandError as exc:
            logger.debug("Skipped expired remote %s %s: %s", kind, row.id, exc)


def _expired_remote_rows(db: HubDatabase) -> list[tuple[WorkspaceKind, Workspace]]:
    rows: list[tuple[WorkspaceKind, Workspace]] = []
    machine_id = require_machine_id()
    now = datetime.now(UTC)
    for kind, table in _TABLE.items():
        sql = f"""
            SELECT w.* FROM {table} w
            WHERE w.status = 'merged' AND w.machine_id != %s
              AND w.cleanup_after < %s
        """  # nosec B608
        sql += CLOSED_TASK_CLEANUP_PREDICATE
        sql += " ORDER BY w.cleanup_after ASC, w.id ASC LIMIT %s"
        rows.extend(
            (kind, _from_row(kind, row))
            for row in db.fetchall(sql, (machine_id, now, 1000 - len(rows)))
        )
        if len(rows) == 1000:
            break
    return rows


def _matches(db: HubDatabase, kind: WorkspaceKind, ref: str) -> list[Any]:
    """Return up to two rows, on any machine, that the reference ``ref`` names."""
    text = ref.strip()
    full = parse_uuid_reference(text)
    if full is not None:
        sql = f"SELECT id, machine_id FROM {_TABLE[kind]} WHERE id = %s"  # nosec B608
        return db.fetchall(sql, (str(full),))
    if kind == "worktree" and _UUID_TEXT_PREFIX_RE.fullmatch(text) is not None:
        sql = (
            "SELECT id, machine_id FROM worktrees WHERE id::text LIKE %s ESCAPE '\\' "
            "ORDER BY id LIMIT 2"
        )
        return db.fetchall(sql, (f"{_escape_like_prefix(text)}%",))
    return []


def owner_machine(db: HubDatabase, kind: WorkspaceKind, ref: str) -> Owner | None:
    """Return ``(full_id, machine_id)`` for the one row ``ref`` names on any machine."""
    rows = _matches(db, kind, ref)
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
    agent_session_id: str | None = None,
    limit: int | None = None,
) -> list[Any]:
    conditions = ["machine_id = %s"]
    params: list[Any] = [machine_id]
    if project_id:
        conditions.append("project_id = %s")
        params.append(project_id)
    if status:
        conditions.append("status = %s")
        params.append(status)
    if agent_session_id:
        conditions.append("agent_session_id = %s")
        params.append(agent_session_id)
    where_clause = " AND ".join(conditions)
    sql = f"SELECT * FROM {_TABLE[kind]} WHERE {where_clause} ORDER BY created_at DESC"  # nosec B608
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
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
    """Return the refusal for a machine the caller does not own, or None when allowed.

    An omitted machine is the hub's own, so it is checked like any named machine.
    """
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
    """Authorize every tool of one kind; get, merge and delete of a remote row go to its node.

    The list tools keep their own machine check, which names the requested machine.
    """
    dispatch: dict[str, Callable[..., Any]] = {
        f"{op}_{kind}": remote
        for op, remote in (
            ("get", _remote_get),
            ("merge", _remote_merge),
            ("delete", _remote_delete),
        )
    }
    for name in dispatch:
        if registry.get_tool_metadata(name) is None:
            raise LookupError(f"{name} is not registered on {registry.name}")
    for entry in registry.list_tools():
        name = entry["name"]
        tool = registry.get_tool_metadata(name)
        if tool is None or name in _LIST_TOOLS:
            continue
        registry.register(
            name,
            tool.description,
            tool.input_schema,
            _guarded(tool.func, kind=kind, db=db, remote=dispatch.get(name)),
            brief=tool.brief,
            output_schema=tool.output_schema,
            read_only=tool.read_only,
        )


def _guarded(
    original: Callable[..., Any],
    *,
    kind: WorkspaceKind,
    db: HubDatabase,
    remote: Callable[..., Any] | None,
) -> Callable[..., Any]:
    """Ownership first. A remote row goes to its node when ``remote`` is given; otherwise the original runs."""
    if inspect.iscoroutinefunction(original):

        @functools.wraps(original)
        async def guarded_async(**kwargs: Any) -> Any:
            try:
                owner = _owned(db, kind, kwargs)
                if remote is None or owner is None or is_local_machine(owner[1]):
                    return await original(**kwargs)
                result = remote(db, kind, kwargs, owner)
                return await result if inspect.isawaitable(result) else result
            except NodeCommandError as exc:
                return node_error_result(exc)

        return guarded_async

    @functools.wraps(original)
    def guarded_sync(**kwargs: Any) -> Any:
        try:
            owner = _owned(db, kind, kwargs)
            if remote is None or owner is None or is_local_machine(owner[1]):
                return original(**kwargs)
            return remote(db, kind, kwargs, owner)
        except NodeCommandError as exc:
            return node_error_result(exc)

    return guarded_sync


def _owned(db: HubDatabase, kind: WorkspaceKind, kwargs: dict[str, Any]) -> Owner | None:
    """Check the owner of the row a call names before the original runs.

    A path-addressed call, or a reference that names no single row, is checked against the
    hub's machine. A bound caller gets a generic refusal for an ambiguous prefix, so no
    other machine's row ids reach the error.
    """
    ref = kwargs.get(f"{kind}_id")
    if not isinstance(ref, str):
        require_machine_owned(None)
        return None
    rows = _matches(db, kind, ref)
    owner = (str(rows[0]["id"]), str(rows[0]["machine_id"])) if len(rows) == 1 else None
    require_machine_owned(owner[1] if owner is not None else None)
    if owner is None and len(rows) > 1 and current_caller() is not None:
        raise NodeCommandError(
            "ambiguous_reference",
            "Ambiguous workspace reference; pass the full workspace UUID to disambiguate.",
            stage="none",
        )
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
    if _flag(kwargs.get("push")):
        return _refused("remote_merge_option_unsupported", option="push")
    if _flag(kwargs.get("prefer_remote")):
        return _refused("remote_merge_option_unsupported", option="prefer_remote")
    source = kwargs.get("source_branch")
    if source is not None and source != row.branch_name:
        return _refused("remote_merge_option_unsupported", option="source_branch")
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


def _refused(code: str, **fields: Any) -> dict[str, Any]:
    return {"success": False, "error_code": code, **fields}


def _not_found(kind: WorkspaceKind, workspace_id: str) -> dict[str, Any]:
    return {"success": False, "error": f"{kind} '{workspace_id}' not found"}


def _flag(value: Any) -> bool:
    return value in (True, "true", "True", "1")
