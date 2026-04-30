"""MCP build tool surface."""

from __future__ import annotations

import warnings
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Literal

from gobby.build.service import BuildOptions, build
from gobby.mcp_proxy.tools.internal import InternalToolRegistry

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext

BuildProfileName = Literal[
    "quick",
    "review",
    "full",
    "default_unattended",
    "full-unattended",
    "default_yolo",
    "full-yolo",
    "auto",
]
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
        unattended: bool = False,
        composer_yolo: bool = False,
        yolo: bool | None = None,
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
        if yolo is not None:
            warnings.warn(
                "build_task.yolo is deprecated; use unattended for dispatch automation",
                DeprecationWarning,
                stacklevel=2,
            )
            if not unattended:
                unattended = bool(yolo)

        opts = BuildOptions(
            profile=profile,
            skip_stages=skip_stages or [],
            isolation=isolation,
            unattended=unattended,
            composer_yolo=composer_yolo,
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
                    "enum": [
                        "quick",
                        "review",
                        "full",
                        "default_unattended",
                        "full-unattended",
                        "default_yolo",
                        "full-yolo",
                        "auto",
                    ],
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
                "unattended": {"type": "boolean", "default": False},
                "composer_yolo": {"type": "boolean", "default": False},
                "yolo": {"type": "boolean", "deprecated": True},
                "max_review_rounds": {"type": "integer", "default": 3, "minimum": 1},
                "max_expansion_attempts": {"type": "integer", "minimum": 1},
                "max_qa_rounds": {"type": "integer", "minimum": 1},
                "max_merge_attempts": {"type": "integer", "minimum": 1},
                "max_holistic_rounds": {"type": "integer", "minimum": 1},
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
