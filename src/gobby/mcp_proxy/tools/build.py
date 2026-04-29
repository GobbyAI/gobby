"""MCP build tool surface."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Literal

from gobby.build.service import BuildOptions, build
from gobby.mcp_proxy.tools.internal import InternalToolRegistry

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext

BuildProfileName = Literal["quick", "review", "full", "full-yolo", "auto"]
BuildIsolation = Literal["none", "worktree", "clone"]


def create_build_registry(ctx: RegistryContext) -> InternalToolRegistry:
    """Create the build tool registry."""

    registry = InternalToolRegistry(
        name="gobby-build",
        description="Build automation tools",
    )

    async def build_task(
        input_ref: str,
        profile: BuildProfileName = "auto",
        skip_stages: list[str] | None = None,
        isolation: BuildIsolation = "worktree",
        yolo: bool = False,
        max_review_rounds: int = 3,
        max_expansion_attempts: int | None = None,
        max_qa_rounds: int | None = None,
        max_merge_attempts: int | None = None,
        max_holistic_rounds: int | None = None,
        target_branch: str | None = None,
        agent: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Start lifecycle automation for a plan file, epic, or automated leaf task."""

        resolved_project_id = project_id or ctx.get_current_project_id()
        if resolved_project_id is None:
            raise ValueError("Could not determine project_id for build_task")

        opts = BuildOptions(
            profile=profile,
            skip_stages=skip_stages or [],
            isolation=isolation,
            yolo=yolo,
            max_review_rounds=max_review_rounds,
            max_expansion_attempts=max_expansion_attempts,
            max_qa_rounds=max_qa_rounds,
            max_merge_attempts=max_merge_attempts,
            max_holistic_rounds=max_holistic_rounds,
            target_branch=target_branch,
            assigned_agent=agent,
        )
        result = await build(
            input_ref,
            opts,
            db=ctx.task_manager.db,
            project_id=resolved_project_id,
        )
        return asdict(result)

    registry.register(
        name="build_task",
        description=("Start lifecycle automation for a plan file, epic, or automated leaf task."),
        input_schema={
            "type": "object",
            "properties": {
                "input_ref": {"type": "string"},
                "profile": {
                    "type": "string",
                    "enum": ["quick", "review", "full", "full-yolo", "auto"],
                    "default": "auto",
                },
                "skip_stages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
                "isolation": {
                    "type": "string",
                    "enum": ["none", "worktree", "clone"],
                    "default": "worktree",
                },
                "yolo": {"type": "boolean", "default": False},
                "max_review_rounds": {"type": "integer", "default": 3},
                "max_expansion_attempts": {"type": "integer"},
                "max_qa_rounds": {"type": "integer"},
                "max_merge_attempts": {"type": "integer"},
                "max_holistic_rounds": {"type": "integer"},
                "target_branch": {"type": "string"},
                "agent": {"type": "string"},
                "project_id": {"type": "string"},
            },
            "required": ["input_ref"],
        },
        func=build_task,
    )

    return registry


__all__ = ["create_build_registry"]
