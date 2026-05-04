"""MCP build tool surface."""

from __future__ import annotations

import warnings
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Literal

from gobby.app_context import get_app_context
from gobby.build.service import BuildOptions, build
from gobby.config.build import StageCapOverride
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

DISPATCHER_CRON_DISABLED_MESSAGE = (
    "dispatcher_cron_disabled: dispatcher cron is disabled. "
    "Run `gobby build resume` to re-enable build automation."
)


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
        stage_caps: list[dict[str, Any]] | None = None,
        target_branch: str | None = None,
        agent: str | None = None,
        reset_expansion_output: bool = False,
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
            stage_caps=_stage_caps_from_payload(stage_caps or []),
            target_branch=target_branch,
            assigned_agent=agent,
            reset_expansion_output=reset_expansion_output,
        )
        result = await build(
            input_ref,
            opts,
            db=ctx.task_manager.db,
            project_id=resolved_project_id,
            services=get_app_context(),
        )
        payload = asdict(result)
        dispatcher_tick = payload.get("dispatcher_tick")
        if (
            isinstance(dispatcher_tick, dict)
            and dispatcher_tick.get("reason") == "dispatcher_cron_disabled"
        ):
            payload["dispatcher_cron_disabled"] = True
            payload["message"] = DISPATCHER_CRON_DISABLED_MESSAGE
        return payload

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
                "stage_caps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "stage_name": {"type": "string"},
                            "max_work_attempts": {"type": "integer", "minimum": 1},
                            "max_review_rounds": {"type": "integer", "minimum": 1},
                        },
                        "required": ["stage_name"],
                    },
                },
                "target_branch": {"type": "string"},
                "agent": {"type": "string"},
                "reset_expansion_output": {"type": "boolean", "default": False},
                "project_id": {"type": "string"},
            },
            "required": ["input_ref"],
        },
        func=build_task,
    )

    return registry


def _stage_caps_from_payload(payload: list[dict[str, Any]]) -> list[StageCapOverride]:
    return [
        StageCapOverride(
            stage_name=str(item["stage_name"]),
            max_work_attempts=item.get("max_work_attempts"),
            max_review_rounds=item.get("max_review_rounds"),
        )
        for item in payload
    ]


__all__ = ["StageCapOverride", "create_build_registry"]
