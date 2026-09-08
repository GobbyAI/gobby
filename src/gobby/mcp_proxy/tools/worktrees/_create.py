"""Create worktree tool handler."""

from __future__ import annotations

from typing import Any, Literal

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.worktrees._context import RegistryContext
from gobby.mcp_proxy.tools.worktrees._helpers import (
    copy_project_json_to_worktree,
    generate_worktree_path,
    install_provider_hooks,
    resolve_project_context,
)
from gobby.worktrees.creation import create_worktree as create_worktree_record
from gobby.worktrees.events import emit_worktree_event


def create_create_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create a registry with the create_worktree tool.

    Args:
        ctx: Shared registry context

    Returns:
        InternalToolRegistry with create_worktree registered
    """
    registry = InternalToolRegistry(
        name="gobby-worktrees-create",
        description="Worktree creation",
    )

    @registry.tool(
        name="create_worktree",
        description="Create a new git worktree for isolated development.",
    )
    async def create_worktree(
        branch_name: str,
        base_branch: str = "main",
        task_id: str | None = None,
        worktree_path: str | None = None,
        create_branch: bool = True,
        use_local: bool | None = None,
        project_path: str | None = None,
        provider: Literal["claude", "qwen", "codex", "droid"] | None = None,
    ) -> dict[str, Any]:
        """Create a new git worktree.

        Args:
            branch_name: Name for the new branch.
            base_branch: Branch to base the worktree on (default: main).
            task_id: Optional task ID to link to this worktree.
            worktree_path: Optional custom path (defaults to ../{branch_name}).
            create_branch: Whether to create a new branch (default: True).
            use_local: If True, branch from local ref instead of origin/ (preserves unpushed commits).
                       If None (default), auto-detects: uses local when base_branch has unpushed commits.
            project_path: Path to project directory (pass cwd from CLI).
            provider: CLI provider to install hooks for (claude, qwen, codex, droid).
                     If specified, installs hooks so agents can communicate with daemon.

        Returns:
            Dict with worktree ID, path, and branch info.
        """
        if base_branch.startswith(("origin/", "refs/remotes/")):
            return {
                "success": False,
                "error": f"Remote-style base branch is not allowed: {base_branch}",
                "error_code": "remote_base_branch_not_allowed",
            }
        resolved_git_mgr, resolved_project_id, error = resolve_project_context(
            project_path, ctx.git_manager, ctx.project_id
        )
        if error:
            return {"success": False, "error": error}

        if resolved_git_mgr is None or resolved_project_id is None:
            raise RuntimeError("Git manager or project ID unexpectedly None")

        result = await create_worktree_record(
            git_manager=resolved_git_mgr,
            worktree_storage=ctx.worktree_storage,
            project_id=resolved_project_id,
            branch_name=branch_name,
            base_branch=base_branch,
            task_id=task_id,
            worktree_path=worktree_path,
            create_branch=create_branch,
            use_local=use_local,
            provider=provider,
            resolve_task_id=ctx.resolve_task_id,
            path_factory=generate_worktree_path,
            sidecar_writer=copy_project_json_to_worktree,
            hook_installer=install_provider_hooks,
            event_emitter=emit_worktree_event,
        )
        if not result.success:
            response: dict[str, Any] = {"success": False, "error": result.error}
            if result.error_code == "remote_base_branch_not_allowed":
                response["error_code"] = result.error_code
            if result.existing_worktree_id is not None:
                response["existing_worktree_id"] = result.existing_worktree_id
                response["existing_path"] = result.existing_path
            return response

        if result.worktree is None:
            raise RuntimeError("Successful worktree creation returned no worktree")
        return {
            "success": True,
            "worktree_id": result.worktree.id,
            "worktree_path": result.worktree.worktree_path,
            "hooks_installed": result.hooks_installed,
            "event": result.event,
        }

    return registry
