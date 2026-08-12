"""Spawn and persona MCP tool registration for agents."""

from __future__ import annotations

from typing import Any

from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry


def register_agent_spawn_tools(
    registry: InternalToolRegistry,
    ctx: AgentsRegistryContext,
) -> None:
    @registry.tool(
        name="evaluate_spawn",
        description="Dry-run evaluation of spawn_agent. Defaults parent_session_id to current session.",
    )
    async def evaluate_spawn_tool(
        agent: str = "default",
        workflow: str | None = None,
        task_id: str | None = None,
        isolation: str | None = None,
        provider: str | None = None,
        branch_name: str | None = None,
        base_branch: str | None = None,
        parent_session_id: str | None = None,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        from gobby.agents.dry_run import evaluate_spawn

        effective_parent_ref = parent_session_id or ctx.get_current_session_id()

        resolved_parent = None
        if effective_parent_ref:
            try:
                resolved_parent = ctx.resolve_session_id(effective_parent_ref)
            except ValueError:
                resolved_parent = effective_parent_ref

        if not project_path:
            project_ctx = ctx.get_project_context()
            if project_ctx:
                context_project_path = project_ctx.get("project_path")
                if isinstance(context_project_path, str):
                    project_path = context_project_path

        eval_result = await evaluate_spawn(
            agent=agent,
            workflow=workflow,
            task_id=task_id,
            isolation=isolation,
            provider=provider,
            branch_name=branch_name,
            base_branch=base_branch,
            parent_session_id=resolved_parent,
            project_path=project_path,
            db=ctx.db,
            workflow_loader=ctx.workflow_loader,
            runner=ctx.runner,
            session_manager=ctx.session_manager,
            git_manager=ctx.git_manager,
            worktree_storage=ctx.worktree_storage,
            clone_storage=ctx.clone_storage,
            clone_manager=ctx.clone_manager,
            task_manager=ctx.task_manager,
            mcp_manager=ctx.mcp_inventory,
        )
        return eval_result.to_dict()

    from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry

    spawn_registry = create_spawn_agent_registry(
        runner=ctx.runner,
        task_manager=ctx.task_manager,
        worktree_storage=ctx.worktree_storage,
        git_manager=ctx.git_manager,
        clone_storage=ctx.clone_storage,
        clone_manager=ctx.clone_manager,
        session_manager=ctx.session_manager,
        db=ctx.db,
        completion_registry=ctx.completion_registry,
        config_resolver=lambda: ctx.daemon_config,
        code_index=ctx.code_index,
        detection_registry=ctx.detection_registry,
    )

    registry.merge_from(spawn_registry)

    @registry.tool(
        name="apply_persona",
        description=(
            "Apply a persona-capable agent definition to the current session. "
            "Updates prompt-facing persona state and skill selection without "
            "spawning a child agent or changing provider/model/isolation."
        ),
    )
    async def apply_persona(
        agent: str,
        variables: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        from gobby.mcp_proxy.tools.apply_persona import apply_persona_impl

        return await apply_persona_impl(
            agent=agent,
            db=ctx.db,
            variables=variables,
            task_id=task_id,
            task_manager=ctx.task_manager,
        )
