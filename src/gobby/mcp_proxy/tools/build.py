"""MCP build tool surface."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Literal

from gobby.app_context import get_app_context
from gobby.build.options import resolve_build_isolation
from gobby.build.service import BuildOptions, build
from gobby.config.build import Isolation, StageCapOverride
from gobby.mcp_proxy.tools.internal import InternalToolRegistry

if TYPE_CHECKING:
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext

WorkspaceBackend = Literal["worktree", "clone"]

AUTOMATION_DISABLED_MESSAGE = (
    "automation_disabled: project build automation is paused. "
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
        quick: bool = False,
        skip_stages: list[str] | None = None,
        isolation: Isolation | None = None,
        workspace_backend: WorkspaceBackend | None = None,
        clone: bool = False,
        no_merge: bool = False,
        pr: str | None = None,
        stage: list[str] | None = None,
        target_branch: str | None = None,
        agent: str | None = None,
        reset_expansion_output: bool = False,
        max_active_agents: int | None = None,
        max_retries: int | None = None,
        coordinator: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Start lifecycle automation for a plan file, epic, or automated leaf task."""

        resolved_project_id = project_id or ctx.get_current_project_id()
        if resolved_project_id is None:
            raise ValueError("Could not determine project_id for build_task")
        resolved_isolation = resolve_build_isolation(
            isolation=isolation,
            workspace_backend=workspace_backend,
            clone=clone,
        )

        opts = BuildOptions(
            quick=quick,
            skip_stages=skip_stages or [],
            skip_stages_explicit=skip_stages is not None,
            isolation=resolved_isolation.isolation,
            isolation_explicit=resolved_isolation.explicit,
            no_merge=no_merge,
            pr=pr,
            stage_caps=_stage_caps_from_payload(stage or []),
            target_branch=target_branch,
            assigned_agent=agent,
            reset_expansion_output=reset_expansion_output,
            max_active_agents=max_active_agents,
            max_retries=max_retries,
            coordinator_session_ref=coordinator,
        )
        result = await build(
            input_ref,
            opts,
            db=ctx.task_manager.db,
            project_id=resolved_project_id,
            services=get_app_context(),
        )
        payload = asdict(result)
        if not payload.get("warnings"):
            payload.pop("warnings", None)
        dispatcher_tick = payload.get("dispatcher_tick")
        if (
            isinstance(dispatcher_tick, dict)
            and dispatcher_tick.get("reason") == "automation_disabled"
        ):
            payload["automation_disabled"] = True
            payload["message"] = AUTOMATION_DISABLED_MESSAGE
        return payload

    registry.register(
        name="build_task",
        description=("Start lifecycle automation for a plan file, epic, or automated leaf task."),
        input_schema={
            "type": "object",
            "properties": {
                "input_ref": {"type": "string"},
                "quick": {"type": "boolean", "default": False},
                "skip_stages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
                "isolation": {
                    "type": "string",
                    "enum": ["none", "worktree", "clone"],
                },
                "workspace_backend": {
                    "type": "string",
                    "enum": ["worktree", "clone"],
                },
                "clone": {"type": "boolean", "default": False},
                "no_merge": {"type": "boolean", "default": False},
                "pr": {"type": "string"},
                "stage": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Stage selector/settings, e.g. development:max_review_rounds=4",
                },
                "target_branch": {"type": "string"},
                "agent": {"type": "string"},
                "reset_expansion_output": {"type": "boolean", "default": False},
                "max_active_agents": {"type": "integer", "minimum": 1},
                "max_retries": {"type": "integer", "minimum": 0},
                "coordinator": {"type": "string"},
                "project_id": {"type": "string"},
            },
            "required": ["input_ref"],
        },
        func=build_task,
    )

    return registry


def _stage_caps_from_payload(payload: list[str]) -> list[StageCapOverride]:
    parsed: dict[str, dict[str, int | None]] = {}
    for raw in payload:
        stage_name, separator, settings_text = raw.partition(":")
        stage_name = stage_name.strip()
        if not stage_name:
            raise ValueError("stage name is required")
        settings = parsed.setdefault(stage_name, {})
        if not separator:
            continue
        for item in (part.strip() for part in settings_text.split(",") if part.strip()):
            key, key_separator, value_text = item.partition("=")
            if not key_separator:
                raise ValueError("stage setting must use name=value")
            key = key.strip()
            if key not in {"max_work_attempts", "max_review_rounds"}:
                raise ValueError("stage setting must be max_work_attempts or max_review_rounds")
            try:
                settings[key] = int(value_text)
            except ValueError as exc:
                raise ValueError("stage setting value must be an integer") from exc
    return [
        StageCapOverride(
            stage_name=stage_name,
            max_work_attempts=settings.get("max_work_attempts"),
            max_review_rounds=settings.get("max_review_rounds"),
        )
        for stage_name, settings in parsed.items()
    ]


__all__ = ["StageCapOverride", "create_build_registry"]
