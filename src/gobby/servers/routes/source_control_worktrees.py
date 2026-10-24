"""Worktree and clone routes for the source-control API."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from gobby.clones.git import CloneGitManager
from gobby.hub import remote_workspaces
from gobby.hub.caller_scope import require_machine_owned
from gobby.hub.node_client import NodeCommandError
from gobby.mcp_proxy.tools.clones import create_clones_registry
from gobby.servers.routes import source_control_git
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from gobby.utils.machine_id import require_machine_id
from gobby.worktrees import git as worktree_git
from gobby.worktrees.containment import path_is_within
from gobby.worktrees.creation import create_worktree as create_worktree_record
from gobby.worktrees.deletion import (
    DeletionSurface,
    WorktreeDeletionRequest,
    delete_worktree_transaction,
)

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

type GitRefValidator = Callable[[str, str], None]


class CreateClientWorktreeRequest(BaseModel):
    """Validated payload for a client-owned worktree."""

    project_id: str
    branch_name: str
    base_branch: str | None = None
    workspace_role: Literal["client"]


def create_source_control_worktrees_router(
    server: HTTPServer,
    *,
    validate_git_ref: GitRefValidator,
) -> APIRouter:
    """Create source-control worktree and clone routes."""
    router = APIRouter()

    async def authorize_machine(machine_id: str | None) -> None:
        try:
            await server.run_db(require_machine_owned, machine_id)
        except NodeCommandError as exc:
            raise HTTPException(
                status_code=403, detail={"error_code": exc.code, "stage": "none"}
            ) from exc

    async def workspace_owner(kind: remote_workspaces.WorkspaceKind, ref: str) -> tuple[str, str]:
        owner = cast(
            tuple[str, str] | None,
            await server.run_db(
                remote_workspaces.owner_machine, server.services.database, kind, ref
            ),
        )
        if owner is None:
            raise HTTPException(404, f"{kind.capitalize()} not found")
        await authorize_machine(owner[1])
        return owner

    async def remove_node_workspace(
        kind: remote_workspaces.WorkspaceKind, workspace_id: str, merged_into: str | None = None
    ) -> dict[str, Any]:
        row = await server.run_db(
            remote_workspaces.get_remote, server.services.database, kind, workspace_id
        )
        if row is None:
            raise HTTPException(404, f"{kind.capitalize()} not found")
        try:
            result = await remote_workspaces.remove_remote(
                server.services.database,
                kind,
                row,
                force=False,
                force_delete_branch=False,
                merged_into=merged_into,
                client=remote_workspaces.node_command_client(),
            )
        except NodeCommandError as exc:
            raise HTTPException(
                status_code=503 if exc.code == "node_offline" else 502,
                detail={"error_code": exc.code, "stage": exc.stage},
            ) from exc
        if not result["success"]:
            raise HTTPException(
                status_code=409,
                detail={"success": False, "id": workspace_id, "error_code": result["error_code"]},
            )
        return {"success": True, "id": workspace_id, "git_deleted": True}

    @router.get("/worktrees")
    async def list_worktrees(
        project_id: str | None = None,
        status: str | None = None,
        machine_id: str | None = None,
    ) -> dict[str, Any]:
        """List worktrees."""
        await authorize_machine(machine_id)
        if machine_id is not None and machine_id != require_machine_id():
            worktrees = await server.run_db(
                remote_workspaces.list_remote,
                server.services.database,
                "worktree",
                machine_id,
                project_id=project_id,
                status=status,
            )
            return {"worktrees": [worktree.to_dict() for worktree in worktrees]}
        if not server.services.worktree_storage:
            return {"worktrees": []}

        worktrees = await server.run_db(
            server.services.worktree_storage.list_worktrees,
            project_id=project_id,
            status=status,
        )
        return {"worktrees": [worktree.to_dict() for worktree in worktrees]}

    @router.post("/worktrees")
    async def create_client_worktree(body: CreateClientWorktreeRequest) -> dict[str, Any]:
        """Create a client-owned worktree with no task binding."""
        validate_git_ref(body.branch_name, "branch_name")
        if body.base_branch is not None:
            validate_git_ref(body.base_branch, "base_branch")
        worktree_storage = server.services.worktree_storage
        if worktree_storage is None:
            raise HTTPException(503, "Worktree storage not available")

        repo_path = cast(
            str | None,
            await server.run_db(source_control_git._resolve_project, server, body.project_id),
        )
        if repo_path is None:
            raise HTTPException(404, "Project not found")

        git_manager = worktree_git.WorktreeGitManager(repo_path)
        result = await create_worktree_record(
            git_manager=git_manager,
            worktree_storage=worktree_storage,
            project_id=body.project_id,
            branch_name=body.branch_name,
            base_branch=body.base_branch,
            workspace_role=body.workspace_role,
        )
        if not result.success:
            status_code = 409 if result.error_code == "branch_conflict" else 400
            raise HTTPException(
                status_code,
                detail={"error_code": result.error_code, "message": result.error},
            )
        if result.worktree is None:
            raise RuntimeError("Successful worktree creation returned no worktree")
        return result.worktree.to_dict()

    @router.get("/worktrees/stats")
    async def get_worktree_stats(
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Get worktree statistics."""
        if not server.services.worktree_storage or not project_id:
            return {"stats": {}}

        stats = await server.run_db(server.services.worktree_storage.count_by_status, project_id)
        return {"stats": stats}

    @router.delete("/worktrees/{worktree_id}")
    async def delete_worktree(
        worktree_id: str,
        merged_into: str | None = None,
    ) -> dict[str, Any]:
        """Delete a worktree after checking client terminal ownership."""
        worktree_storage = server.services.worktree_storage
        if worktree_storage is None:
            raise HTTPException(503, "Worktree storage not available")

        owner = await workspace_owner("worktree", worktree_id)
        if owner[1] != require_machine_id():
            return await remove_node_workspace("worktree", owner[0], merged_into)

        try:
            worktree = await server.run_db(worktree_storage.get, worktree_id)
        except MachineOwnershipMismatchError as exc:
            raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
        if worktree is None:
            raise HTTPException(404, "Worktree not found")

        if worktree.workspace_role == "client":
            terminal_ids = await _live_client_terminal_ids(server, worktree)
            if terminal_ids:
                raise HTTPException(
                    status_code=409,
                    detail={"error_code": "terminals_live", "terminal_ids": terminal_ids},
                )

        def resolve_git_manager(candidate: Any) -> Any:
            fallback = server.services.git_manager
            if fallback is None:
                return None
            try:
                candidate_repo_path = source_control_git._resolve_project(
                    server, candidate.project_id
                )
                if candidate_repo_path:
                    return worktree_git.WorktreeGitManager(candidate_repo_path)
            except (ValueError, OSError):
                pass
            return fallback

        request = WorktreeDeletionRequest(
            worktree_id=worktree_id,
            surface=DeletionSurface.HTTP,
            merged_into=merged_into,
        )
        try:
            result = await server.services.run_worktree_delete(
                lambda boundary: delete_worktree_transaction(
                    boundary,
                    request=request,
                    worktree_storage=worktree_storage,
                    resolve_git_manager=resolve_git_manager,
                    task_manager=server.services.task_manager,
                )
            )
        except MachineOwnershipMismatchError as exc:
            raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
        if not result.found:
            raise HTTPException(404, "Worktree not found")

        response: dict[str, Any] = {
            "success": result.success,
            "id": worktree_id,
            "git_deleted": result.git_deleted,
        }
        if not result.git_deleted:
            response["git_error"] = result.error
            response["message"] = "Git worktree deletion failed; DB record was preserved"
        if result.error_code:
            response["error_code"] = result.error_code
        if not result.success:
            raise HTTPException(status_code=409, detail=response)
        return response

    @router.post("/worktrees/cleanup")
    async def cleanup_worktrees(
        project_id: str | None = None,
        hours: int = 24,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Cleanup stale worktrees."""
        if not server.services.worktree_storage or not project_id:
            return {"candidates": [], "cleaned": 0}

        stale = await server.run_db(
            server.services.worktree_storage.cleanup_stale,
            project_id,
            hours=hours,
            dry_run=dry_run,
        )
        return {
            "candidates": [worktree.to_dict() for worktree in stale],
            "cleaned": 0 if dry_run else len(stale),
            "dry_run": dry_run,
        }

    @router.post("/worktrees/{worktree_id}/sync")
    async def sync_worktree(
        worktree_id: str,
        source_branch: str | None = None,
    ) -> dict[str, Any]:
        """Sync a worktree with its base branch."""
        if not server.services.worktree_storage:
            raise HTTPException(503, "Worktree storage not available")

        try:
            worktree = await server.run_db(server.services.worktree_storage.get, worktree_id)
        except MachineOwnershipMismatchError as exc:
            raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
        if not worktree:
            raise HTTPException(404, "Worktree not found")

        if server.services.git_manager:
            result = await server.services.git_manager.sync_from_main(
                worktree.worktree_path,
                base_branch=worktree.base_branch,
                source_branch=source_branch,
            )
            response = {
                "success": result.success,
                "message": result.message,
                "id": worktree_id,
                "source_branch": source_branch or worktree.base_branch,
            }
            if not result.success:
                raise HTTPException(status_code=409, detail=response)
            return response

        raise HTTPException(503, "Git manager not available")

    @router.get("/clones")
    async def list_clones(
        project_id: str | None = None,
        machine_id: str | None = None,
    ) -> dict[str, Any]:
        """List clones."""
        await authorize_machine(machine_id)
        if machine_id is not None and machine_id != require_machine_id():
            clones = await server.run_db(
                remote_workspaces.list_remote,
                server.services.database,
                "clone",
                machine_id,
                project_id=project_id,
            )
            return {"clones": [clone.to_dict() for clone in clones]}
        if not server.services.clone_storage:
            return {"clones": []}

        clones = await server.run_db(
            server.services.clone_storage.list_clones, project_id=project_id
        )
        return {"clones": [clone.to_dict() for clone in clones]}

    @router.delete("/clones/{clone_id}")
    async def delete_clone(clone_id: str) -> dict[str, Any]:
        """Delete clone files before retiring their record."""
        return await run_clone_operation(clone_id, "delete_clone")

    @router.post("/clones/{clone_id}/sync")
    async def sync_clone(clone_id: str) -> dict[str, Any]:
        """Pull the clone's configured upstream through the managed Git operation."""
        return await run_clone_operation(clone_id, "sync_clone")

    async def run_clone_operation(clone_id: str, tool_name: str) -> dict[str, Any]:
        if not server.services.clone_storage:
            raise HTTPException(503, "Clone storage not available")

        owner = await workspace_owner("clone", clone_id)
        local_machine = require_machine_id()
        if owner[1] != local_machine:
            if tool_name == "delete_clone":
                return await remove_node_workspace("clone", owner[0])
            refusal = MachineOwnershipMismatchError(
                resource_kind="clone",
                resource_id=owner[0],
                owner_machine_id=owner[1],
                current_machine_id=local_machine,
            )
            raise HTTPException(status_code=409, detail=refusal.to_dict())

        try:
            clone = await server.run_db(server.services.clone_storage.get, clone_id)
        except MachineOwnershipMismatchError as exc:
            raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
        if not clone:
            raise HTTPException(404, "Clone not found")

        try:
            repo_path = await server.run_db(
                source_control_git._resolve_project, server, clone.project_id
            )
            if not repo_path:
                raise HTTPException(503, "Clone project checkout not available")
            git_manager = CloneGitManager(repo_path)
            registry = create_clones_registry(
                clone_storage=server.services.clone_storage,
                git_manager=git_manager,
                project_id=clone.project_id,
            )
            result = await registry.call(tool_name, {"clone_id": clone_id})
        except MachineOwnershipMismatchError as exc:
            raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not result.get("success"):
            raise HTTPException(status_code=409, detail=result)
        return {**result, "id": clone_id}

    return router


async def _live_client_terminal_ids(server: HTTPServer, worktree: Any) -> list[str]:
    """Return live Gobby terminal IDs whose sessions use the client worktree."""
    terminal_manager = getattr(server.services, "terminal_manager", None)
    session_manager = server.session_manager
    if terminal_manager is None or session_manager is None:
        return []

    def find_ids() -> list[str]:
        terminal_ids: list[str] = []
        for terminal in terminal_manager.list_by_project(worktree.project_id):
            if terminal.ownership != "gobby" or terminal.state != "live" or not terminal.session_id:
                continue
            session = session_manager.get(terminal.session_id)
            workspace_path = getattr(session, "workspace_path", None)
            if isinstance(workspace_path, str) and path_is_within(
                workspace_path, worktree.worktree_path
            ):
                terminal_ids.append(terminal.id)
        return terminal_ids

    return cast(list[str], await server.run_db(find_ids))
